"""Clients for the three systems of record. Everything upstream-shaped stops here:
callers get normalized dicts with our field names and canonical account ids."""

import asyncio
import csv
import io
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

from app.ids import canonical_account_id
from app.logs import correlation_id, log
from app.resilience import CircuitBreaker, UpstreamError, call_upstream

logger = logging.getLogger("app.upstreams")

TIERS = {"Platinum", "Gold", "Silver", "Standard"}
STATUSES = {"open", "in_progress", "pending_customer", "escalated", "resolved", "closed"}


class UpstreamNotFound(Exception):
    pass


class UpstreamConflict(Exception):
    pass


@dataclass(frozen=True)
class Entitlement:
    account_id: str
    name: str
    tier: str
    contract_end: date
    renewal_date: date
    arr_usd: int


@dataclass(frozen=True)
class EntitlementSnapshot:
    accounts: dict[str, Entitlement]
    as_of: datetime
    stale: bool = False


# ---- normalization ------------------------------------------------------------

def _lower_first(key: str) -> str:
    return key[:1].lower() + key[1:]


def normalize_case(raw: dict) -> dict:
    """The case system mixes 'Status'/'status' and 'Comments'/'comments'. Fold the keys,
    canonicalize the account id, and rename timestamp fields to ours."""
    r = {_lower_first(k): v for k, v in raw.items()}
    case_id = r["id"]
    comments = []
    for i, c in enumerate(r.get("comments") or [], start=1):
        c = {_lower_first(k): v for k, v in c.items()}
        item = {"id": c.get("id") or f"{case_id}-C{i}", "author": c["author"], "body": c["body"], "createdAt": c["at"]}
        if c.get("via"):
            item["via"] = c["via"]
        comments.append(item)
    status = r["status"]
    if status not in STATUSES:
        log(logger, logging.WARNING, "unknown case status from upstream", case_id=case_id, status=status)
    out = {
        "id": case_id,
        "accountId": canonical_account_id(r["accountId"]),
        "priority": r["priority"],
        "status": status,
        "openedAt": r["openedAt"],
        "subject": r["subject"],
        "comments": comments,
        "modstamp": r.get("systemModstamp") or r["openedAt"],
    }
    if r.get("escalation"):
        out["escalation"] = r["escalation"]
    return out


def parse_entitlements(csv_text: str, xml_text: str | None) -> dict[str, Entitlement]:
    """CSV is the nightly export (system of record snapshot). The XML change feed is
    newer, so where it has tier/renewal for an account, it wins."""
    feed: dict[str, dict] = {}
    if xml_text:
        root = ET.fromstring(xml_text)
        for acc in root.iter("account"):
            try:
                acc_id = canonical_account_id(acc.get("id", ""))
            except ValueError:
                continue
            feed[acc_id] = {"tier": acc.findtext("tier"), "renewal": acc.findtext("renewal")}

    out: dict[str, Entitlement] = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        try:
            acc_id = canonical_account_id(row["account_id"])
            contract_end = date.fromisoformat(row["contract_end"])
            arr = int(row["arr_usd"])
        except (KeyError, ValueError):
            log(logger, logging.WARNING, "skipping bad entitlement row", row=row.get("account_id"))
            continue
        tier, renewal = row["support_tier"], contract_end
        f = feed.get(acc_id)
        if f:
            if f["tier"] and f["tier"] != tier:
                log(logger, logging.INFO, "feed overrides tier", account_id=acc_id, csv=tier, feed=f["tier"])
                tier = f["tier"]
            if f["renewal"]:
                renewal = date.fromisoformat(f["renewal"])
        if tier not in TIERS:
            log(logger, logging.WARNING, "unknown support tier", account_id=acc_id, tier=tier)
            continue
        out[acc_id] = Entitlement(acc_id, row["name"], tier, contract_end, renewal, arr)
    return out


# ---- client -----------------------------------------------------------------------

class Upstreams:
    def __init__(self, settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http
        self.breakers = {name: CircuitBreaker(name) for name in ("casesys", "entitlement", "warehouse")}
        self._ent_cache: EntitlementSnapshot | None = None
        self._ent_cached_at = 0.0
        self._ent_lock = asyncio.Lock()

    async def _send(self, system: str, method: str, url: str, *, retry: bool = True, **kw) -> httpx.Response:
        headers = {"X-Correlation-ID": correlation_id.get(), **kw.pop("headers", {})}

        async def once() -> httpx.Response:
            started = time.perf_counter()
            try:
                resp = await self.http.request(method, url, headers=headers, timeout=self.s.upstream_timeout_s, **kw)
            except httpx.TimeoutException:
                raise UpstreamError(system, "timeout")
            except httpx.TransportError as e:
                raise UpstreamError(system, f"connection error: {type(e).__name__}")
            log(logger, logging.DEBUG, "upstream call", upstream=system, method=method, url=url,
                status=resp.status_code, ms=round((time.perf_counter() - started) * 1000, 1))
            if resp.status_code >= 500:
                raise UpstreamError(system, f"HTTP {resp.status_code}")
            return resp

        return await call_upstream(self.breakers[system], once, attempts=self.s.upstream_retries if retry else 1)

    # case system

    async def list_cases(self, *, status: list[str] | None = None, priority: list[str] | None = None,
                         account_ids: list[str] | None = None) -> list[dict]:
        params = {}
        if status:
            params["status"] = ",".join(status)
        if priority:
            params["priority"] = ",".join(priority)
        resp = await self._send("casesys", "GET", f"{self.s.casesys_url}/cases", params=params)
        resp.raise_for_status()
        cases = [normalize_case(r) for r in resp.json()["records"]]
        if account_ids:
            # Upstream stores ids unpadded/padded inconsistently, so filter after canonicalizing.
            wanted = set(account_ids)
            cases = [c for c in cases if c["accountId"] in wanted]
        return cases

    async def get_case(self, case_id: str) -> dict:
        resp = await self._send("casesys", "GET", f"{self.s.casesys_url}/cases/{case_id}")
        if resp.status_code == 404:
            raise UpstreamNotFound(case_id)
        resp.raise_for_status()
        return normalize_case(resp.json())

    async def add_comment(self, case_id: str, *, author: str, body: str, external_id: str, via: str | None) -> dict:
        # Safe to retry: the case system dedupes on externalId.
        payload = {"author": author, "body": body, "externalId": external_id}
        if via:
            payload["via"] = via
        resp = await self._send("casesys", "POST", f"{self.s.casesys_url}/cases/{case_id}/comments", json=payload)
        if resp.status_code == 404:
            raise UpstreamNotFound(case_id)
        resp.raise_for_status()
        c = resp.json()
        out = {"id": c["id"], "author": c["author"], "body": c["body"], "createdAt": c["at"]}
        if c.get("via"):
            out["via"] = c["via"]
        return out

    async def update_case(self, case_id: str, fields: dict, *, expected_modstamp: str) -> dict:
        # Not retried: if the write landed but the response was lost, a retry would
        # see a new modstamp and report a false conflict.
        resp = await self._send("casesys", "PATCH", f"{self.s.casesys_url}/cases/{case_id}",
                                json={**fields, "expectedModstamp": expected_modstamp}, retry=False)
        if resp.status_code == 404:
            raise UpstreamNotFound(case_id)
        if resp.status_code == 409:
            raise UpstreamConflict(case_id)
        resp.raise_for_status()
        return normalize_case(resp.json())

    # entitlement system

    async def entitlements(self) -> EntitlementSnapshot:
        """Whole-dataset fetch with a 5 min cache. If the entitlement system is down,
        serve the last good copy for up to 24h, flagged as stale."""
        async with self._ent_lock:
            age = time.monotonic() - self._ent_cached_at
            if self._ent_cache and age < self.s.entitlement_cache_ttl_s:
                return self._ent_cache
            try:
                csv_resp = await self._send("entitlement", "GET", f"{self.s.entitlement_url}/export/accounts.csv")
                csv_resp.raise_for_status()
                xml_text = None
                try:
                    xml_resp = await self._send("entitlement", "GET", f"{self.s.entitlement_url}/feeds/accounts.xml",
                                                retry=False)
                    if xml_resp.status_code == 200:
                        xml_text = xml_resp.text
                except UpstreamError:
                    log(logger, logging.WARNING, "entitlement feed unavailable, using CSV only")
                snap = EntitlementSnapshot(parse_entitlements(csv_resp.text, xml_text), datetime.now(UTC))
            except UpstreamError:
                if self._ent_cache and age < self.s.entitlement_max_stale_s:
                    log(logger, logging.WARNING, "serving stale entitlements", age_s=int(age))
                    return EntitlementSnapshot(self._ent_cache.accounts, self._ent_cache.as_of, stale=True)
                raise
            self._ent_cache, self._ent_cached_at = snap, time.monotonic()
            return snap

    # usage warehouse

    async def recent_usage(self, account_id: str, months: int = 2) -> list[dict]:
        statement = (
            "SELECT month, protected_tb, api_calls, active_users FROM usage_monthly"
            " WHERE account_id = ? ORDER BY month DESC LIMIT ?"
        )
        body = {"statement": statement,
                "bindings": [{"type": "TEXT", "value": account_id}, {"type": "FIXED", "value": months}]}
        resp = await self._send("warehouse", "POST", f"{self.s.warehouse_url}/api/v2/statements", json=body)
        resp.raise_for_status()
        data = resp.json()
        cols = [c["name"] for c in data["resultSetMetaData"]["rowType"]]
        rows = [dict(zip(cols, r)) for r in data["data"]]
        return [
            {"month": r["month"], "protectedTb": float(r["protected_tb"]),
             "apiCalls": int(r["api_calls"]), "activeUsers": int(r["active_users"])}
            for r in rows
        ]
