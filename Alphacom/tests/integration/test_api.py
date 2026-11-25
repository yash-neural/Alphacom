"""Behaviour of the API end to end (in-process upstreams, real MongoDB)."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app import webhooks
from app.models import AuditLog
from tests.conftest import FINANCE, SUPPORT

UNRESOLVED = "open,in_progress,pending_customer,escalated"


def sample_question_params() -> dict:
    """'Platinum customers with P1 cases open > 48h and a renewal in the next 90 days'."""
    return {
        "priority": "P1",
        "status": UNRESOLVED,
        "customerTier": "Platinum",
        "openedBefore": (datetime.now(UTC) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "renewalBefore": (date.today() + timedelta(days=90)).isoformat(),
    }


# ---- auth ----

async def test_missing_token_is_401_problem(client):
    r = await client.get("/v1/cases")
    assert r.status_code == 401
    assert r.headers["content-type"] == "application/problem+json"
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert r.json()["type"].endswith("/unauthorized")


async def test_wrong_scope_is_403(client, auth):
    r = await client.post("/v1/cases/CS-10231/comments", json={"body": "hi"},
                          headers={**await auth(["cases:read"]), "Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 403
    assert "cases:write" in r.json()["detail"]


async def test_garbage_token_is_401(client):
    r = await client.get("/v1/cases", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


async def test_token_exchange_never_exceeds_user_or_agent_ceiling(client, token):
    finance_user = await token(FINANCE + ["webhooks:manage"])
    r = await client.post("/dev/token/exchange", json={"subject_token": finance_user, "actor": "support-copilot"})
    granted = set(r.json()["scope"].split())
    # User has no write/escalate, agent ceiling has no financials/webhooks.
    assert granted == {"customers:read", "cases:read"}

    delegated = r.json()["access_token"]
    r = await client.post("/dev/token/exchange", json={"subject_token": delegated, "actor": "support-copilot"})
    assert r.status_code == 400  # no re-delegation


# ---- customers ----

async def test_customer_aggregate_hides_arr_without_finance_scope(client, auth):
    r = await client.get("/v1/customers/ACC-7", headers=await auth(SUPPORT))
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "ACC-007"
    assert "arrUsd" not in body and body["redactedFields"] == ["arrUsd"]
    assert body["usage"]["latestMonth"] == "2026-10"
    assert body["openCases"]["byPriority"]["P1"] == 1
    assert body["degraded"] is False


async def test_customer_shows_arr_to_finance(client, auth):
    r = await client.get("/v1/customers/ACC-007", headers=await auth(FINANCE))
    assert r.json()["arrUsd"] == 1250000
    assert r.json()["redactedFields"] == []


async def test_customer_degrades_when_warehouse_down(client, auth, upstreams):
    upstreams.FAULTS["warehouse"] = {"mode": "error"}
    r = await client.get("/v1/customers/ACC-044", headers=await auth())
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True and body["degradedSources"] == ["warehouse"]
    assert body["usage"] is None
    assert body["supportTier"] == "Platinum"  # entitlement still there


async def test_customer_degrades_when_warehouse_slow(client, auth, upstreams):
    upstreams.FAULTS["warehouse"] = {"mode": "ok", "latency_ms": 3000}
    started = datetime.now()
    r = await client.get("/v1/customers/ACC-044", headers=await auth())
    assert r.json()["degradedSources"] == ["warehouse"]
    assert (datetime.now() - started).total_seconds() < 2.5  # bounded by the optional-source budget


async def test_customer_503_when_entitlement_down_and_no_cache(client, auth, upstreams):
    upstreams.FAULTS["entitlement"] = {"mode": "error"}
    r = await client.get("/v1/customers/ACC-007", headers=await auth())
    assert r.status_code == 503
    assert r.json()["upstream"] == "entitlement"
    assert "retry-after" in r.headers


async def test_customer_served_stale_from_cache_when_entitlement_goes_down(make_client, auth, upstreams):
    client = await make_client(entitlement_cache_ttl_s=0)
    tok = (await client.post("/dev/token", json={"sub": "a", "scopes": SUPPORT})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert (await client.get("/v1/customers/ACC-007", headers=h)).status_code == 200  # warms cache
    upstreams.FAULTS["entitlement"] = {"mode": "error"}
    r = await client.get("/v1/customers/ACC-007", headers=h)
    assert r.status_code == 200
    assert r.json()["dataFreshness"]["entitlementStale"] is True
    assert "entitlement" in r.json()["degradedSources"]


async def test_unknown_customer_404_and_bad_id_400(client, auth):
    h = await auth()
    assert (await client.get("/v1/customers/ACC-999", headers=h)).status_code == 404
    r = await client.get("/v1/customers/CUST-1", headers=h)
    assert r.status_code == 400
    assert r.json()["errors"][0]["field"] == "customerId"


async def test_customer_etag_304(client, auth):
    h = await auth()
    r = await client.get("/v1/customers/ACC-007", headers=h)
    r2 = await client.get("/v1/customers/ACC-007", headers={**h, "If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304


# ---- cases ----

async def test_sample_question_returns_exactly_the_right_cases(client, auth):
    r = await client.get("/v1/cases", params=sample_question_params(), headers=await auth())
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()["data"]}
    # CS-10260 excluded: XML feed says Halden renewed. CS-10247: opened < 48h ago.
    # CS-10266: Gold. CS-10262: renewal too far. CS-10288: no entitlement record.
    assert ids == {"CS-10231", "CS-10240", "CS-10251"}


async def test_cursor_pagination_walks_everything_once(client, auth):
    h = await auth()
    seen, cursor = [], None
    while True:
        params = {"limit": 4, "sort": "priority"}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/v1/cases", params=params, headers=h)).json()
        seen += [c["id"] for c in page["data"]]
        cursor = page["page"]["nextCursor"]
        if not cursor:
            break
    everything = (await client.get("/v1/cases", params={"limit": 100}, headers=h)).json()["data"]
    assert sorted(seen) == sorted(c["id"] for c in everything)
    assert len(seen) == len(set(seen))


async def test_cursor_rejected_with_different_filters(client, auth):
    h = await auth()
    page = (await client.get("/v1/cases", params={"limit": 2}, headers=h)).json()
    r = await client.get("/v1/cases", params={"limit": 2, "priority": "P1", "cursor": page["page"]["nextCursor"]}, headers=h)
    assert r.status_code == 400 and r.json()["type"].endswith("/invalid-cursor")


async def test_invalid_filters_are_400_with_field_errors(client, auth):
    r = await client.get("/v1/cases", params={"priority": "P9", "status": "nope", "limit": 500}, headers=await auth())
    assert r.status_code == 400
    fields = {e["field"] for e in r.json()["errors"]}
    assert {"limit"} <= fields or {"priority", "status"} <= fields


async def test_deprecated_account_param_still_works_with_headers(client, auth):
    r = await client.get("/v1/cases", params={"account": "ACC-77"}, headers=await auth())
    assert r.status_code == 200
    assert r.headers["deprecation"] == "true" and "sunset" in r.headers
    assert {c["accountId"] for c in r.json()["data"]} == {"ACC-077"}


async def test_cases_503_when_case_system_down(client, auth, upstreams):
    upstreams.FAULTS["casesys"] = {"mode": "error"}
    r = await client.get("/v1/cases", headers=await auth())
    assert r.status_code == 503 and r.json()["upstream"] == "casesys"


async def test_case_system_blip_is_retried(client, auth, upstreams):
    upstreams.FAULTS["casesys"] = {"mode": "error", "fail_count": 1}
    r = await client.get("/v1/cases", headers=await auth())
    assert r.status_code == 200


# ---- comments ----

async def test_comment_is_idempotent(client, auth):
    h = {**await auth(), "Idempotency-Key": str(uuid.uuid4())}
    r1 = await client.post("/v1/cases/CS-10231/comments", json={"body": "Engineering engaged"}, headers=h)
    r2 = await client.post("/v1/cases/CS-10231/comments", json={"body": "Engineering engaged"}, headers=h)
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    assert r2.headers["idempotent-replayed"] == "true"
    case = (await client.get("/v1/cases/CS-10231", headers=h)).json()
    assert sum(c["body"] == "Engineering engaged" for c in case["comments"]) == 1
    assert r1.json()["author"] == "alice"


async def test_idempotency_key_reuse_with_other_body_is_422(client, auth):
    h = {**await auth(), "Idempotency-Key": str(uuid.uuid4())}
    await client.post("/v1/cases/CS-10231/comments", json={"body": "one"}, headers=h)
    r = await client.post("/v1/cases/CS-10231/comments", json={"body": "two"}, headers=h)
    assert r.status_code == 422


async def test_idempotency_keys_are_per_caller(client, auth):
    key = str(uuid.uuid4())
    r1 = await client.post("/v1/cases/CS-10231/comments", json={"body": "x"}, headers={**await auth(sub="alice"), "Idempotency-Key": key})
    r2 = await client.post("/v1/cases/CS-10231/comments", json={"body": "x"}, headers={**await auth(sub="bob"), "Idempotency-Key": key})
    assert r1.json()["id"] != r2.json()["id"]
    assert "idempotent-replayed" not in r2.headers


async def test_comment_requires_idempotency_key_and_valid_body(client, auth):
    h = await auth()
    assert (await client.post("/v1/cases/CS-10231/comments", json={"body": "x"}, headers=h)).status_code == 400
    h["Idempotency-Key"] = str(uuid.uuid4())
    r = await client.post("/v1/cases/CS-10231/comments", json={"body": "x", "author": "ceo"}, headers=h)
    assert r.status_code == 400  # can't spoof author: unknown fields rejected
    r = await client.post("/v1/cases/CS-10231/comments", json={"body": ""}, headers=h)
    assert r.status_code == 400


async def test_failed_upstream_frees_idempotency_key_for_retry(client, auth, upstreams):
    h = {**await auth(), "Idempotency-Key": str(uuid.uuid4())}
    upstreams.FAULTS["casesys"] = {"mode": "error"}
    assert (await client.post("/v1/cases/CS-10231/comments", json={"body": "retry me"}, headers=h)).status_code == 503
    upstreams.FAULTS.clear()
    client.app.state.upstreams.breakers["casesys"].on_success()
    r = await client.post("/v1/cases/CS-10231/comments", json={"body": "retry me"}, headers=h)
    assert r.status_code == 201 and "idempotent-replayed" not in r.headers


async def test_comment_by_agent_is_marked_via(client, token):
    user = await token(SUPPORT)
    delegated = (await client.post("/dev/token/exchange", json={"subject_token": user, "actor": "support-copilot"})).json()["access_token"]
    r = await client.post("/v1/cases/CS-10231/comments", json={"body": "from copilot"},
                          headers={"Authorization": f"Bearer {delegated}", "Idempotency-Key": str(uuid.uuid4())})
    assert r.json()["author"] == "alice" and r.json()["via"] == "support-copilot"


# ---- escalation ----

async def _etag(client, h, case_id):
    return (await client.get(f"/v1/cases/{case_id}", headers=h)).headers["etag"]


ESC = {"reason": "customer_impact", "note": "Customer cannot restore finance share"}


async def test_escalate_happy_path_emits_signed_webhook(client, auth):
    sub = await webhooks.create_subscription(client.app.state.db, "http://receiver/hooks", ["case.escalated"], "t",
                                             secret="whsec_local_demo_secret")
    h = await auth()
    r = await client.post("/v1/cases/CS-10240/escalate", json=ESC,
                          headers={**h, "If-Match": await _etag(client, h, "CS-10240")})
    assert r.status_code == 200
    assert r.json()["status"] == "escalated" and r.json()["escalation"]["escalatedBy"] == "alice"

    sent = await webhooks.dispatch_due(client.app.state.db, client.app.state.http)
    assert sent == 1
    from examples.webhook_receiver import seen
    (event,) = seen.values()
    assert event["data"]["caseId"] == "CS-10240" and event["type"] == "case.escalated"
    assert sub["secret"] == "whsec_local_demo_secret"


async def test_escalate_rules(client, auth):
    h = await auth()
    assert (await client.post("/v1/cases/CS-10240/escalate", json=ESC, headers=h)).status_code == 428
    r = await client.post("/v1/cases/CS-10240/escalate", json=ESC, headers={**h, "If-Match": '"stale"'})
    assert r.status_code == 412
    r = await client.post("/v1/cases/CS-10270/escalate", json=ESC, headers={**h, "If-Match": await _etag(client, h, "CS-10270")})
    assert r.status_code == 409 and r.json()["type"].endswith("/case-not-escalatable")
    r = await client.post("/v1/cases/CS-10285/escalate", json=ESC, headers={**h, "If-Match": await _etag(client, h, "CS-10285")})
    assert r.status_code == 409 and r.json()["type"].endswith("/already-escalated")
    # P4 for a Standard-tier customer
    r = await client.post("/v1/cases/CS-10278/escalate", json=ESC, headers={**h, "If-Match": await _etag(client, h, "CS-10278")})
    assert r.status_code == 422 and r.json()["type"].endswith("/escalation-not-allowed")
    # P3 for a Platinum customer is fine
    r = await client.post("/v1/cases/CS-10272/escalate", json=ESC, headers={**h, "If-Match": await _etag(client, h, "CS-10272")})
    assert r.status_code == 200


async def test_escalate_twice_with_same_etag_only_escalates_once(client, auth):
    h = await auth()
    etag = await _etag(client, h, "CS-10231")
    r1 = await client.post("/v1/cases/CS-10231/escalate", json=ESC, headers={**h, "If-Match": etag})
    r2 = await client.post("/v1/cases/CS-10231/escalate", json=ESC, headers={**h, "If-Match": etag})
    assert (r1.status_code, r2.status_code) == (200, 412)


async def test_escalate_invalid_reason_is_400(client, auth):
    h = await auth()
    r = await client.post("/v1/cases/CS-10231/escalate", json={"reason": "because", "note": "please escalate"},
                          headers={**h, "If-Match": await _etag(client, h, "CS-10231")})
    assert r.status_code == 400


async def test_escalations_are_rate_limited_per_user(make_client, upstreams):
    client = await make_client(escalations_per_hour=1)
    tok = (await client.post("/dev/token", json={"sub": "a", "scopes": SUPPORT})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    await client.post("/v1/cases/CS-10231/escalate", json=ESC, headers={**h, "If-Match": await _etag(client, h, "CS-10231")})
    r = await client.post("/v1/cases/CS-10240/escalate", json=ESC, headers={**h, "If-Match": await _etag(client, h, "CS-10240")})
    assert r.status_code == 429 and "retry-after" in r.headers


# ---- platform ----

async def test_rate_limit_headers_and_429(make_client, upstreams):
    client = await make_client(rate_limit_per_min=2)
    tok = (await client.post("/dev/token", json={"sub": "a", "scopes": SUPPORT})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    r1 = await client.get("/v1/cases", headers=h)
    assert r1.headers["ratelimit-limit"] == "2" and r1.headers["ratelimit-remaining"] == "1"
    await client.get("/v1/cases", headers=h)
    r3 = await client.get("/v1/cases", headers=h)
    assert r3.status_code == 429 and r3.json()["type"].endswith("/rate-limited")


async def test_correlation_id_propagates_and_every_call_is_audited(client, auth):
    h = {**await auth(), "X-Correlation-ID": "test-corr-123"}
    r = await client.get("/v1/cases/CS-10231", headers=h)
    assert r.headers["x-correlation-id"] == "test-corr-123"
    await client.get("/v1/cases", headers={"X-Correlation-ID": "denied-corr"})
    async with client.app.state.db.session() as session:
        ok = (await session.scalars(select(AuditLog).where(AuditLog.correlation_id == "test-corr-123"))).one()
        denied = (await session.scalars(select(AuditLog).where(AuditLog.correlation_id == "denied-corr"))).one()
    assert ok.subject == "alice" and ok.resource == "case:CS-10231" and ok.outcome == "success"
    assert denied.outcome == "denied" and denied.http_status == 401


async def test_audit_log_cannot_be_changed_by_the_app(client, auth):
    await client.get("/v1/cases", headers=await auth())
    async with client.app.state.db.session() as session:
        for stmt in ("DELETE FROM audit_log", "UPDATE audit_log SET outcome = 'success'"):
            with pytest.raises(DBAPIError, match="permission denied"):
                async with session.begin():
                    await session.execute(text(stmt))
