import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from enum import Enum
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app import webhooks
from app.auth import Principal, require
from app.idempotency import request_hash
from app.logs import correlation_id, log
from app.pagination import decode_cursor, encode_cursor, filter_fingerprint
from app.problems import PROBLEM_JSON, ApiProblem, not_found, upstream_unavailable, validation_problem
from app.resilience import UpstreamError
from app.routes.customers import parse_account_id
from app.db import iso, utcnow
from app.upstreams import STATUSES, EntitlementSnapshot, UpstreamConflict, UpstreamNotFound

logger = logging.getLogger("app.cases")
router = APIRouter(tags=["Cases"])

CASE_ID = r"^CS-[0-9]{4,10}$"
PRIORITIES = ["P1", "P2", "P3", "P4"]
TIERS = ["Platinum", "Gold", "Silver", "Standard"]


class SortOrder(str, Enum):
    opened_asc = "openedAt"
    opened_desc = "-openedAt"
    priority_asc = "priority"
    priority_desc = "-priority"


class EscalationReason(str, Enum):
    customer_impact = "customer_impact"
    sla_breach_risk = "sla_breach_risk"
    security_incident = "security_incident"
    executive_request = "executive_request"


class NewComment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=4000)


class EscalationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: EscalationReason
    note: str = Field(min_length=10, max_length=1000)


# ---- shaping -------------------------------------------------------------------------

def case_etag(case: dict) -> str:
    return '"' + hashlib.sha256(f"{case['id']}:{case['modstamp']}".encode()).hexdigest()[:20] + '"'


def customer_ref(snap: EntitlementSnapshot | None, account_id: str) -> dict | None:
    ent = snap.accounts.get(account_id) if snap else None
    if ent is None:
        return None
    return {"id": ent.account_id, "name": ent.name, "supportTier": ent.tier, "renewalDate": ent.renewal_date.isoformat()}


def case_summary(case: dict, snap: EntitlementSnapshot | None) -> dict:
    return {
        "id": case["id"], "accountId": case["accountId"], "priority": case["priority"],
        "status": case["status"], "openedAt": case["openedAt"], "subject": case["subject"],
        "customer": customer_ref(snap, case["accountId"]),
    }


def case_detail(case: dict, snap: EntitlementSnapshot | None) -> dict:
    out = case_summary(case, snap)
    out["comments"] = case["comments"]
    if case.get("escalation"):
        out["escalation"] = case["escalation"]
    return out


def _csv_enum(raw: str | None, allowed: list[str], field: str, errors: list) -> list[str] | None:
    if raw is None:
        return None
    values = [v.strip() for v in raw.split(",") if v.strip()]
    bad = [v for v in values if v not in allowed]
    if bad or not values:
        errors.append({"location": "query", "field": field,
                       "message": f"allowed values: {', '.join(allowed)}; got {', '.join(bad) or 'nothing'}"})
    return values


async def _entitlements_or_none(request: Request, required: bool) -> EntitlementSnapshot | None:
    try:
        return await request.app.state.upstreams.entitlements()
    except UpstreamError as e:
        if required:
            raise upstream_unavailable("entitlement", f"Tier/renewal filters need the entitlement system ({e.reason}).")
        log(logger, logging.WARNING, "entitlements unavailable; cases returned without customer block")
        return None


# ---- list ------------------------------------------------------------------------------

def _sort_key(sort: SortOrder):
    if sort in (SortOrder.opened_asc, SortOrder.opened_desc):
        return lambda c: [c["openedAt"], c["id"]]
    return lambda c: [int(c["priority"][1:]), c["id"]]


@router.get("/v1/cases")
async def list_cases(
    request: Request,
    status: str | None = None,
    priority: str | None = None,
    accountId: Annotated[str | None, Query(max_length=20)] = None,
    account: Annotated[str | None, Query(max_length=20, deprecated=True)] = None,
    customerTier: str | None = None,
    openedBefore: datetime | None = None,
    openedAfter: datetime | None = None,
    renewalBefore: date | None = None,
    sort: SortOrder = SortOrder.opened_desc,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    _: Principal = Depends(require("cases:read")),
):
    errors: list[dict] = []
    statuses = _csv_enum(status, sorted(STATUSES), "status", errors)
    priorities = _csv_enum(priority, PRIORITIES, "priority", errors)
    tiers = _csv_enum(customerTier, TIERS, "customerTier", errors)
    for name, dt in (("openedBefore", openedBefore), ("openedAfter", openedAfter)):
        if dt is not None and dt.tzinfo is None:
            errors.append({"location": "query", "field": name, "message": "must include a timezone, e.g. 2026-11-23T12:00:00Z"})
    if errors:
        raise validation_problem(errors)

    deprecated_param = account is not None and accountId is None
    acc = parse_account_id(accountId or account, "accountId", "query") if (accountId or account) else None

    filters = {"status": statuses, "priority": priorities, "accountId": acc, "customerTier": tiers,
               "openedBefore": openedBefore, "openedAfter": openedAfter, "renewalBefore": renewalBefore,
               "sort": sort.value}
    fingerprint = filter_fingerprint(filters)
    secret = request.app.state.settings.cursor_secret
    after = decode_cursor(secret, cursor, fingerprint) if cursor else None

    up = request.app.state.upstreams
    try:
        cases = await up.list_cases(status=statuses, priority=priorities, account_ids=[acc] if acc else None)
    except UpstreamError as e:
        raise upstream_unavailable("casesys", f"The case system is unavailable ({e.reason}).")
    snap = await _entitlements_or_none(request, required=bool(tiers or renewalBefore))

    def keep(c: dict) -> bool:
        opened = datetime.fromisoformat(c["openedAt"].replace("Z", "+00:00"))
        if openedBefore and not opened < openedBefore:
            return False
        if openedAfter and not opened > openedAfter:
            return False
        if tiers or renewalBefore:
            ent = snap.accounts.get(c["accountId"]) if snap else None
            if ent is None:
                return False
            if tiers and ent.tier not in tiers:
                return False
            if renewalBefore and ent.renewal_date > renewalBefore:
                return False
        return True

    key = _sort_key(sort)
    desc = sort.value.startswith("-")
    rows = sorted((c for c in cases if keep(c)), key=key, reverse=desc)
    if after is not None:
        rows = [c for c in rows if (key(c) < after if desc else key(c) > after)]
    page, more = rows[:limit], len(rows) > limit

    body = {
        "data": [case_summary(c, snap) for c in page],
        "page": {"limit": limit, "nextCursor": encode_cursor(secret, key(page[-1]), fingerprint) if more else None},
    }
    headers = {}
    if deprecated_param:
        headers = {"Deprecation": "true", "Sunset": "Wed, 30 Jun 2027 00:00:00 GMT",
                   "Link": '</v1/cases?accountId=>; rel="deprecation"'}
    return JSONResponse(body, headers=headers)


# ---- detail ------------------------------------------------------------------------------

async def _load_case(request: Request, case_id: str) -> dict:
    try:
        return await request.app.state.upstreams.get_case(case_id)
    except UpstreamNotFound:
        raise not_found(f"Case {case_id}")
    except UpstreamError as e:
        raise upstream_unavailable("casesys", f"The case system is unavailable ({e.reason}).")


@router.get("/v1/cases/{caseId}")
async def get_case(
    request: Request,
    caseId: Annotated[str, Path(pattern=CASE_ID)],
    _: Principal = Depends(require("cases:read")),
):
    request.state.resource = f"case:{caseId}"
    case = await _load_case(request, caseId)
    snap = await _entitlements_or_none(request, required=False)
    return JSONResponse(case_detail(case, snap), headers={"ETag": case_etag(case)})


# ---- idempotent writes ------------------------------------------------------------------

Work = Callable[[], Awaitable[tuple[int, dict, dict]]]


async def run_idempotent(request: Request, principal: Principal, key: str | None, body: dict, work: Work):
    """Run work() at most once per (caller, key). 2xx and deterministic 4xx outcomes
    are stored and replayed; 5xx, 429 and 409-in-flight free the key for a retry."""
    if key is None:
        status, payload, headers = await work()
        return JSONResponse(payload, status_code=status, headers=headers)
    if not 8 <= len(key) <= 128:
        raise validation_problem([{"location": "header", "field": "Idempotency-Key", "message": "length must be 8-128"}])

    store = request.app.state.idempotency
    scope = principal.idempotency_scope
    replay = await store.begin(scope, key, request.method, request.url.path, request_hash(request.url.path, body))
    if replay:
        return JSONResponse(replay.body, status_code=replay.status,
                            headers={**replay.headers, "Idempotent-Replayed": "true"},
                            media_type=PROBLEM_JSON if replay.status >= 400 else None)
    try:
        status, payload, headers = await work()
    except ApiProblem as p:
        if p.status < 500 and p.status != 429:
            await store.complete(scope, key, p.status, p.body(request.url.path), p.headers)
        else:
            await store.release(scope, key)
        raise
    except BaseException:
        await store.release(scope, key)
        raise
    await store.complete(scope, key, status, payload, headers)
    return JSONResponse(payload, status_code=status, headers=headers)


@router.post("/v1/cases/{caseId}/comments", status_code=201)
async def add_comment(
    request: Request,
    caseId: Annotated[str, Path(pattern=CASE_ID)],
    body: NewComment,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    principal: Principal = Depends(require("cases:write")),
):
    request.state.resource = f"case:{caseId}"
    if idempotency_key is None:
        raise validation_problem([{"location": "header", "field": "Idempotency-Key",
                                   "message": "required; send a UUID and reuse it on retries"}])

    async def work():
        # externalId lets the case system dedupe too, so a retry after a lost response
        # never double-posts even if our own record was released.
        ext = hashlib.sha256(f"{principal.idempotency_scope}|{idempotency_key}".encode()).hexdigest()[:32]
        try:
            comment = await request.app.state.upstreams.add_comment(
                caseId, author=principal.sub, body=body.body, external_id=ext, via=principal.actor)
        except UpstreamNotFound:
            raise not_found(f"Case {caseId}")
        except UpstreamError as e:
            raise upstream_unavailable("casesys", f"The case system is unavailable ({e.reason}).")
        return 201, comment, {"Location": f"/v1/cases/{caseId}"}

    return await run_idempotent(request, principal, idempotency_key, body.model_dump(), work)


@router.post("/v1/cases/{caseId}/escalate")
async def escalate_case(
    request: Request,
    caseId: Annotated[str, Path(pattern=CASE_ID)],
    body: EscalationRequest,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    principal: Principal = Depends(require("cases:escalate")),
):
    request.state.resource = f"case:{caseId}"
    if not if_match:
        raise ApiProblem(428, "precondition-required", "Precondition required",
                         "Send If-Match with the case's current ETag (from GET /v1/cases/{caseId})")
    settings = request.app.state.settings
    quota = request.app.state.ratelimiter.check(f"escalate:{principal.sub}", settings.escalations_per_hour, 3600)
    if not quota.allowed:
        raise ApiProblem(429, "rate-limited", "Too many escalations",
                         f"At most {settings.escalations_per_hour} escalations per hour per user",
                         headers={"Retry-After": str(max(quota.reset_s, 1))})

    async def work():
        case = await _load_case(request, caseId)
        if case_etag(case) != if_match:
            raise ApiProblem(412, "precondition-failed", "Precondition failed",
                             "The case changed since you read it; GET it again and retry with the new ETag")
        if case["status"] in ("resolved", "closed"):
            raise ApiProblem(409, "case-not-escalatable", "Case cannot be escalated",
                             f"Case is {case['status']}; reopen it first")
        if case["status"] == "escalated":
            raise ApiProblem(409, "already-escalated", "Case already escalated", "Case is already escalated")
        snap = await _entitlements_or_none(request, required=case["priority"] in ("P3", "P4"))
        if case["priority"] in ("P3", "P4"):
            ent = snap.accounts.get(case["accountId"]) if snap else None
            if ent is None or ent.tier not in ("Platinum", "Gold"):
                raise ApiProblem(422, "escalation-not-allowed", "Escalation not allowed",
                                 "P3/P4 cases can only be escalated for Platinum or Gold customers")

        escalation = {"reason": body.reason.value, "note": body.note, "escalatedAt": iso(utcnow()),
                      "escalatedBy": principal.sub}
        if principal.actor:
            escalation["via"] = principal.actor
        try:
            updated = await request.app.state.upstreams.update_case(
                caseId, {"status": "escalated", "escalation": escalation}, expected_modstamp=case["modstamp"])
        except UpstreamConflict:
            raise ApiProblem(412, "precondition-failed", "Precondition failed",
                             "The case changed while escalating; GET it again and retry")
        except UpstreamError as e:
            raise upstream_unavailable("casesys", f"The case system is unavailable ({e.reason}).")

        event = {"caseId": caseId, "accountId": updated["accountId"], "priority": updated["priority"],
                 "reason": body.reason.value, "escalatedBy": principal.sub}
        if principal.actor:
            event["actor"] = principal.actor
        try:
            await webhooks.enqueue(request.app.state.db, "case.escalated", event, correlation_id.get())
        except Exception:
            # The escalation itself succeeded upstream; don't fail the caller. See DECISIONS.md
            # on why the long-term fix is CDC from the case system rather than dual writes.
            logger.exception("failed to enqueue case.escalated")
        return 200, case_detail(updated, snap), {"ETag": case_etag(updated)}

    return await run_idempotent(request, principal, idempotency_key, body.model_dump(mode="json"), work)
