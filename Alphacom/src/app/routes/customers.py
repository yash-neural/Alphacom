import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request, Response
from fastapi.responses import JSONResponse

from app.auth import Principal, require
from app.ids import canonical_account_id
from app.logs import log
from app.problems import not_found, upstream_unavailable, validation_problem
from app.resilience import UpstreamError
from app.db import iso
from app.upstreams import Entitlement

logger = logging.getLogger("app.customers")
router = APIRouter(tags=["Customers"])

UNRESOLVED = ["open", "in_progress", "pending_customer", "escalated"]


def parse_account_id(raw: str, field: str = "customerId", location: str = "path") -> str:
    try:
        return canonical_account_id(raw)
    except ValueError:
        raise validation_problem([{"location": location, "field": field,
                                   "message": "must look like ACC-<digits>, e.g. ACC-007"}])


def _usage_summary(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    latest = rows[0]
    change = None
    if len(rows) > 1 and rows[1]["protectedTb"]:
        change = round((latest["protectedTb"] - rows[1]["protectedTb"]) / rows[1]["protectedTb"] * 100, 1)
    return {"latestMonth": latest["month"], "protectedTb": latest["protectedTb"], "apiCalls": latest["apiCalls"],
            "activeUsers": latest["activeUsers"], "protectedTbChangePct": change}


def _case_summary(cases: list[dict]) -> dict:
    by = {p: 0 for p in ("P1", "P2", "P3", "P4")}
    for c in cases:
        by[c["priority"]] = by.get(c["priority"], 0) + 1
    return {"total": len(cases), "byPriority": by}


def _etag(body: dict) -> str:
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:20]
    return f'W/"{digest}"'


@router.get("/v1/customers/{customerId}")
async def get_customer(
    request: Request,
    customerId: Annotated[str, Path(max_length=20)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    principal: Principal = Depends(require("customers:read")),
):
    account_id = parse_account_id(customerId)
    request.state.resource = f"customer:{account_id}"
    up = request.app.state.upstreams
    settings = request.app.state.settings

    # Entitlement is the core of the resource: without it there is nothing to return.
    try:
        snap = await up.entitlements()
    except UpstreamError as e:
        raise upstream_unavailable("entitlement", f"The entitlement system is unavailable ({e.reason}).")
    ent: Entitlement | None = snap.accounts.get(account_id)
    if ent is None:
        raise not_found(f"Customer {account_id}")

    # Usage and case counts are optional: fetch in parallel, each with its own budget,
    # and degrade instead of failing the whole response.
    budget = settings.optional_source_budget_s
    usage_res, cases_res = await asyncio.gather(
        asyncio.wait_for(up.recent_usage(account_id), budget),
        asyncio.wait_for(up.list_cases(status=UNRESOLVED, account_ids=[account_id]), budget),
        return_exceptions=True,
    )
    degraded: list[str] = []
    for name, res in (("warehouse", usage_res), ("casesys", cases_res)):
        if isinstance(res, BaseException):
            degraded.append(name)
            log(logger, logging.WARNING, "optional source unavailable, degrading", upstream=name,
                error=type(res).__name__, account_id=account_id)
    if snap.stale:
        degraded.append("entitlement")

    today = datetime.now(UTC).date()
    body = {
        "id": ent.account_id,
        "name": ent.name,
        "supportTier": ent.tier,
        "contract": {
            "endDate": ent.contract_end.isoformat(),
            "renewalDate": ent.renewal_date.isoformat(),
            "daysToRenewal": (ent.renewal_date - today).days,
        },
        "usage": None if isinstance(usage_res, BaseException) else _usage_summary(usage_res),
        "openCases": None if isinstance(cases_res, BaseException) else _case_summary(cases_res),
        "degraded": bool(degraded),
        "degradedSources": degraded,
        "redactedFields": [],
        "dataFreshness": {"entitlementAsOf": iso(snap.as_of), "entitlementStale": snap.stale},
    }
    # Field-level authorization: ARR only for Finance. Omitted, and named as redacted.
    if "customers:financials" in principal.scopes:
        body["arrUsd"] = ent.arr_usd
    else:
        body["redactedFields"].append("arrUsd")

    etag = _etag(body)
    headers = {"ETag": etag, "Vary": "Authorization", "Cache-Control": "private, max-age=0"}
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)
