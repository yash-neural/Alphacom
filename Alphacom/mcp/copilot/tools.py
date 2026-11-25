"""The copilot's tools, independent of the MCP transport (so evals can drive them directly).

Design notes (longer version in DECISIONS.md):
  * Tools are shaped around the questions an agent asks, not around REST resources.
    search_cases takes "min_age_hours" and "renewal_within_days" instead of
    timestamps, because models are bad at date arithmetic and good at copying numbers
    out of a question.
  * Enums everywhere the API has them; no free-text filters.
  * Responses are compact rows with derived fields (age_hours, renewal_in_days) the
    model would otherwise have to compute, and never raw upstream payloads.
  * Writes go through confirm(), which asks the human. If the client can't ask, the
    write does not happen.
"""

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from copilot.api import ApiError, PlatformApi
from copilot.guardrails import SessionLimiter, ToolAudit, cap_output, untrusted_text

Priority = Literal["P1", "P2", "P3", "P4"]
Tier = Literal["Platinum", "Gold", "Silver", "Standard"]
State = Literal["unresolved", "resolved", "any"]
SortBy = Literal["oldest_first", "newest_first", "priority"]
Reason = Literal["customer_impact", "sla_breach_risk", "security_incident", "executive_request"]

STATES = {"unresolved": "open,in_progress,pending_customer,escalated", "resolved": "resolved,closed", "any": None}
SORTS = {"oldest_first": "openedAt", "newest_first": "-openedAt", "priority": "priority"}
COMMENT_NAMESPACE = uuid.UUID("6f6d4b0e-3c1a-4c55-9d1e-2a7f8b9c0d11")

MAX_OUTPUT_CHARS = 6000
MAX_ESCALATIONS_PER_SESSION = 3

# Callable the transport provides: show the human a summary, return True only on an
# explicit yes. Must return False (never raise through) when it can't ask.
Confirm = Callable[[str], Awaitable[bool]]


class ToolFailure(Exception):
    """Returned to the model as an error result. Messages are written for the model."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable

    def as_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "retryable": self.retryable}}


def _hours_since(ts: str) -> int:
    return int((datetime.now(UTC) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() // 3600)


def _days_until(d: str) -> int:
    return (date.fromisoformat(d) - datetime.now(UTC).date()).days


def _api_failure(e: ApiError) -> ToolFailure:
    retryable = e.status in (409, 412, 429, 503)
    hints = {
        "insufficient-scope": "The user you act for is not allowed to do this. Tell them; do not retry.",
        "precondition-failed": "The case changed since it was read. Re-read it with get_case and ask the user again.",
        "rate-limited": "Rate limit reached. Stop and tell the user.",
        "upstream-unavailable": "A backend system is down. Tell the user and try later.",
    }
    return ToolFailure(e.slug, f"{e} {hints.get(e.slug, '')}".strip(), retryable)


class CopilotTools:
    def __init__(self, api: PlatformApi, confirm: Confirm, audit: ToolAudit, *,
                 session_id: str | None = None, user: str = "unknown"):
        self.api = api
        self.confirm = confirm
        self.audit = audit
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.user = user
        self.limiter = SessionLimiter({"read": (30, 60.0), "write": (5, 60.0)})
        self.escalations = 0
        self._seq = 0

    # ---- plumbing ---------------------------------------------------------------------

    async def _run(self, tool: str, kind: str, args: dict, fn) -> dict:
        self._seq += 1
        cid = f"mcp-{self.session_id}-{self._seq}"
        started = time.perf_counter()
        outcome = "ok"
        try:
            if not self.limiter.allow(kind):
                raise ToolFailure("rate_limited", f"Too many {kind} calls this minute. Stop and tell the user.")
            result = await fn(cid)
        except ToolFailure as f:
            outcome, result = f.code, f.as_dict()
        except ApiError as e:
            f = _api_failure(e)
            outcome, result = f.code, f.as_dict()
        self.audit.record(session=self.session_id, user=self.user, actor=self.api.actor, tool=tool,
                          args=args, outcome=outcome, correlation_id=cid,
                          ms=round((time.perf_counter() - started) * 1000, 1))
        return result

    # ---- read tools ---------------------------------------------------------------------

    async def search_cases(self, *, priority: list[Priority] | None = None, state: State = "unresolved",
                           customer_tier: list[Tier] | None = None, min_age_hours: int | None = None,
                           renewal_within_days: int | None = None, account_id: str | None = None,
                           sort_by: SortBy = "oldest_first", limit: int = 10, cursor: str | None = None) -> dict:
        args = {k: v for k, v in locals().items() if k != "self" and v is not None}

        async def go(cid: str) -> dict:
            params: dict = {"sort": SORTS[sort_by], "limit": max(1, min(limit, 25))}
            if priority:
                params["priority"] = ",".join(priority)
            if STATES[state]:
                params["status"] = STATES[state]
            if customer_tier:
                params["customerTier"] = ",".join(customer_tier)
            if min_age_hours is not None:
                params["openedBefore"] = (datetime.now(UTC) - timedelta(hours=min_age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if renewal_within_days is not None:
                params["renewalBefore"] = (datetime.now(UTC).date() + timedelta(days=renewal_within_days)).isoformat()
            if account_id:
                params["accountId"] = account_id
            if cursor:
                params["cursor"] = cursor
            body = (await self.api.call("GET", "/v1/cases", params=params, correlation_id=cid)).json()
            rows = []
            for c in body["data"]:
                cust = c.get("customer") or {}
                rows.append({
                    "case_id": c["id"],
                    "account_id": c["accountId"],
                    "customer": cust.get("name"),
                    "tier": cust.get("supportTier"),
                    "priority": c["priority"],
                    "status": c["status"],
                    "age_hours": _hours_since(c["openedAt"]),
                    "renewal_in_days": _days_until(cust["renewalDate"]) if cust.get("renewalDate") else None,
                    "subject": untrusted_text(c["subject"], 120),
                })
            out = {"count": len(rows), "cases": rows, "next_cursor": body["page"]["nextCursor"]}
            return cap_output(out, MAX_OUTPUT_CHARS, "cases")

        return await self._run("search_cases", "read", args, go)

    async def get_customer_summary(self, *, account_id: str) -> dict:
        async def go(cid: str) -> dict:
            c = (await self.api.call("GET", f"/v1/customers/{account_id}", correlation_id=cid)).json()
            usage = c.get("usage")
            out = {
                "account_id": c["id"],
                "name": c["name"],
                "tier": c["supportTier"],
                "renewal_date": c["contract"]["renewalDate"],
                "renewal_in_days": c["contract"]["daysToRenewal"],
                "open_cases": c.get("openCases"),
                "usage_latest_month": None if not usage else {
                    "month": usage["latestMonth"], "protected_tb": usage["protectedTb"],
                    "protected_tb_change_pct": usage["protectedTbChangePct"], "active_users": usage["activeUsers"],
                },
            }
            if c["degradedSources"]:
                out["missing_data"] = c["degradedSources"]
            if "arrUsd" in c["redactedFields"]:
                out["financials"] = "not available to the copilot"
            return out

        return await self._run("get_customer_summary", "read", {"account_id": account_id}, go)

    async def get_case(self, *, case_id: str, max_comments: int = 5) -> dict:
        async def go(cid: str) -> dict:
            c = (await self.api.call("GET", f"/v1/cases/{case_id}", correlation_id=cid)).json()
            comments = []
            for cm in c["comments"][-max(1, min(max_comments, 10)):]:
                is_customer = cm["author"].startswith("customer:")
                entry = {"from": "customer" if is_customer else "staff",
                         "age_hours": _hours_since(cm["createdAt"]), **untrusted_text(cm["body"], 400)}
                if not is_customer:
                    entry["author"] = cm["author"]
                if cm.get("via"):
                    entry["via"] = cm["via"]
                comments.append(entry)
            cust = c.get("customer") or {}
            out = {
                "case_id": c["id"], "account_id": c["accountId"], "customer": cust.get("name"),
                "tier": cust.get("supportTier"), "priority": c["priority"], "status": c["status"],
                "age_hours": _hours_since(c["openedAt"]), "subject": untrusted_text(c["subject"], 200),
                "comments_total": len(c["comments"]), "recent_comments": comments,
            }
            if c.get("escalation"):
                out["escalation"] = {k: c["escalation"][k] for k in ("reason", "escalatedBy", "escalatedAt")}
            return cap_output(out, MAX_OUTPUT_CHARS, "recent_comments")

        return await self._run("get_case", "read", {"case_id": case_id, "max_comments": max_comments}, go)

    # ---- write tools ----------------------------------------------------------------------

    async def add_case_comment(self, *, case_id: str, body: str, confirm: Confirm | None = None) -> dict:
        async def go(cid: str) -> dict:
            text = body.strip()
            if not 1 <= len(text) <= 1000:
                raise ToolFailure("invalid_input", "Comment must be 1-1000 characters.")
            approved = await (confirm or self.confirm)(
                f"Post this comment on {case_id} as {self.user} (via Support Copilot)?\n\n---\n{text}\n---")
            if not approved:
                raise ToolFailure("declined_by_user", "The user did not approve this comment. Do not retry; ask what to change.")
            # Same session + case + text => same key, so a model that retries the call
            # (or a client that retries the request) can't double-post.
            key = str(uuid.uuid5(COMMENT_NAMESPACE, f"{self.session_id}|{case_id}|{text}"))
            r = await self.api.call("POST", f"/v1/cases/{case_id}/comments", json={"body": text},
                                    headers={"Idempotency-Key": key}, correlation_id=cid)
            return {"status": "posted", "comment_id": r.json()["id"], "case_id": case_id,
                    "duplicate_suppressed": r.headers.get("idempotent-replayed") == "true"}

        return await self._run("add_case_comment", "write", {"case_id": case_id, "body": body}, go)

    async def escalate_case(self, *, case_id: str, reason: Reason, justification: str,
                            confirm: Confirm | None = None) -> dict:
        async def go(cid: str) -> dict:
            if self.escalations >= MAX_ESCALATIONS_PER_SESSION:
                raise ToolFailure("session_limit", f"This session already escalated {self.escalations} cases, "
                                  "the most allowed. A person must do any further escalations.")
            if not 20 <= len(justification.strip()) <= 500:
                raise ToolFailure("invalid_input", "justification must be 20-500 characters and explain the impact.")
            r = await self.api.call("GET", f"/v1/cases/{case_id}", correlation_id=cid)
            case, etag = r.json(), r.headers["etag"]
            if case["status"] in ("escalated", "resolved", "closed"):
                raise ToolFailure("not_escalatable", f"{case_id} is {case['status']}; it cannot be escalated.")
            cust = case.get("customer") or {}
            approved = await (confirm or self.confirm)(
                f"ESCALATE {case_id}?\n"
                f"  customer: {cust.get('name', case['accountId'])} ({cust.get('supportTier', 'unknown tier')})\n"
                f"  priority: {case['priority']}, status: {case['status']}, open {_hours_since(case['openedAt'])}h\n"
                f"  reason: {reason}\n  justification: {justification.strip()}\n"
                f"This notifies the escalation team and cannot be undone by the copilot.")
            if not approved:
                raise ToolFailure("declined_by_user", "The user did not approve the escalation. Do not retry.")
            r = await self.api.call("POST", f"/v1/cases/{case_id}/escalate",
                                    json={"reason": reason, "note": justification.strip()},
                                    headers={"If-Match": etag, "Idempotency-Key": str(uuid.uuid4())},
                                    correlation_id=cid)
            self.escalations += 1
            return {"status": "escalated", "case_id": case_id, "escalated_by": r.json()["escalation"]["escalatedBy"]}

        return await self._run("escalate_case", "write",
                               {"case_id": case_id, "reason": reason, "justification": justification}, go)


def to_text(result: dict) -> str:
    return json.dumps(result, separators=(",", ":"), ensure_ascii=False)
