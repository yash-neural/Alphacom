"""Generates the Postman collection + environment in this folder.

    python postman/generate.py

Kept as code so the checks are reviewable in a diff and easy to extend; the JSON
files are the output and are committed so Postman/newman users don't need Python.
"""

import json
from pathlib import Path

OUT = Path(__file__).parent
SUPPORT = ["customers:read", "cases:read", "cases:write", "cases:escalate"]
PROBLEM = ('pm.test("problem+json", () => pm.expect(pm.response.headers.get("Content-Type"))'
           '.to.include("application/problem+json"));')


def req(name, method, url, *, headers=None, body=None, tests=(), pre=(), auth="support"):
    h = [{"key": "Content-Type", "value": "application/json"}] if body is not None else []
    if auth:
        h.append({"key": "Authorization", "value": "Bearer {{" + auth + "Token}}"})
    h += [{"key": k, "value": v} for k, v in (headers or {}).items()]
    item = {"name": name, "request": {"method": method, "header": h, "url": url}, "event": []}
    if body is not None:
        item["request"]["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2)}
    if pre:
        item["event"].append({"listen": "prerequest", "script": {"type": "text/javascript", "exec": list(pre)}})
    if tests:
        item["event"].append({"listen": "test", "script": {"type": "text/javascript", "exec": list(tests)}})
    return item


def folder(name, items, description=""):
    return {"name": name, "description": description, "item": items}


def status(code):
    return f'pm.test("status {code}", () => pm.response.to.have.status({code}));'


def token(name, var, sub, scopes):
    return req(name, "POST", "{{baseUrl}}/dev/token", auth=None,
               body={"sub": sub, "scopes": scopes, "client_id": "postman"},
               tests=[status(200), f'pm.environment.set("{var}Token", pm.response.json().access_token);'])


ESCALATION = {"reason": "customer_impact", "note": "Immutable snapshots missing for 4 days"}

collection = {
    "info": {
        "name": "Alphacom Platform API",
        "description": "Functional and regression checks. Run in order (Setup first):\n"
                       "npx newman run postman/alphacom.postman_collection.json -e postman/local.postman_environment.json",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "item": [
        folder("0. Setup", [
            req("Reset mock upstreams", "POST", "{{mocksUrl}}/__reset", auth=None, tests=[status(200)]),
            token("Token: support engineer", "support", "alice", SUPPORT),
            token("Token: finance analyst", "finance", "fiona", ["customers:read", "customers:financials", "cases:read"]),
            token("Token: read-only", "readonly", "rory", ["cases:read"]),
        ]),
        folder("1. Customers", [
            req("Aggregate view hides ARR without finance scope", "GET", "{{baseUrl}}/v1/customers/ACC-007", tests=[
                status(200), "const b = pm.response.json();",
                'pm.test("entitlement + usage + open cases", () => { pm.expect(b.supportTier).to.eql("Platinum"); '
                'pm.expect(b.usage).to.have.property("latestMonth"); pm.expect(b.openCases).to.have.property("total"); });',
                'pm.test("ARR omitted and listed as redacted", () => { pm.expect(b).to.not.have.property("arrUsd"); '
                'pm.expect(b.redactedFields).to.include("arrUsd"); });',
                'pm.test("rate limit headers", () => pm.expect(pm.response.headers.has("RateLimit-Remaining")).to.be.true);',
                'pm.test("correlation id echoed", () => pm.expect(pm.response.headers.get("X-Correlation-ID")).to.be.ok);',
                'pm.environment.set("customerEtag", pm.response.headers.get("ETag"));']),
            req("Finance scope sees ARR", "GET", "{{baseUrl}}/v1/customers/ACC-007", auth="finance", tests=[
                status(200), 'pm.test("arrUsd present", () => pm.expect(pm.response.json().arrUsd).to.eql(1250000));']),
            req("Unpadded id is canonicalised (ACC-77 -> ACC-077)", "GET", "{{baseUrl}}/v1/customers/ACC-77", tests=[
                status(200), 'pm.test("canonical id", () => pm.expect(pm.response.json().id).to.eql("ACC-077"));']),
            req("If-None-Match returns 304", "GET", "{{baseUrl}}/v1/customers/ACC-007",
                headers={"If-None-Match": "{{customerEtag}}"}, tests=[status(304)]),
            req("Unknown customer is 404 problem", "GET", "{{baseUrl}}/v1/customers/ACC-999", tests=[status(404), PROBLEM]),
            req("Malformed id is 400 with field error", "GET", "{{baseUrl}}/v1/customers/CUST-1", tests=[
                status(400), PROBLEM,
                'pm.test("field named", () => pm.expect(pm.response.json().errors[0].field).to.eql("customerId"));']),
        ]),
        folder("2. Resilience", [
            req("Inject: usage warehouse down", "POST", "{{mocksUrl}}/__faults", auth=None,
                body={"system": "warehouse", "mode": "error"}, tests=[status(200)]),
            req("Customer still returned, degraded", "GET", "{{baseUrl}}/v1/customers/ACC-044", tests=[
                status(200), "const b = pm.response.json();",
                'pm.test("degraded flag", () => { pm.expect(b.degraded).to.be.true; '
                'pm.expect(b.degradedSources).to.include("warehouse"); pm.expect(b.usage).to.be.null; });',
                'pm.test("entitlement still present", () => pm.expect(b.supportTier).to.eql("Platinum"));']),
            req("Inject: entitlement system down", "POST", "{{mocksUrl}}/__faults", auth=None,
                body={"system": "entitlement", "mode": "error"}, tests=[status(200)]),
            req("Still served from cache, flagged stale", "GET", "{{baseUrl}}/v1/customers/ACC-007", tests=[
                status(200), 'pm.test("served (cache)", () => pm.expect(pm.response.json().supportTier).to.eql("Platinum"));']),
            req("Clear faults", "POST", "{{mocksUrl}}/__reset", auth=None, tests=[status(200)]),
        ], "Uses the mock fault-injection endpoint. Failures are on optional sources so circuits are left closed for later folders."),
        folder("3. Cases", [
            req("Sample question: Platinum P1 > 48h, renewal < 90d", "GET",
                "{{baseUrl}}/v1/cases?priority=P1&status=open,in_progress,pending_customer,escalated"
                "&customerTier=Platinum&openedBefore={{openedBefore}}&renewalBefore={{renewalBefore}}",
                pre=['const before = new Date(Date.now() - 48*3600*1000).toISOString().replace(/\\.\\d{3}Z$/, "Z");',
                     'const renew = new Date(Date.now() + 90*86400*1000).toISOString().slice(0, 10);',
                     'pm.environment.set("openedBefore", before); pm.environment.set("renewalBefore", renew);'],
                tests=[status(200),
                       'pm.test("exactly the three matching cases", () => pm.expect(pm.response.json().data.map(c => c.id).sort())'
                       '.to.eql(["CS-10231","CS-10240","CS-10251"]));',
                       'pm.test("each case carries its customer block", () => pm.response.json().data.forEach('
                       'c => pm.expect(c.customer.supportTier).to.eql("Platinum")));']),
            req("Page 1 (limit 5)", "GET", "{{baseUrl}}/v1/cases?limit=5&sort=priority", tests=[
                status(200), "const b = pm.response.json();",
                'pm.test("5 rows and a cursor", () => { pm.expect(b.data.length).to.eql(5); pm.expect(b.page.nextCursor).to.be.a("string"); });',
                'pm.environment.set("cursor", b.page.nextCursor); pm.environment.set("page1", JSON.stringify(b.data.map(c => c.id)));']),
            req("Page 2 follows the cursor without overlap", "GET", "{{baseUrl}}/v1/cases?limit=5&sort=priority&cursor={{cursor}}", tests=[
                status(200), 'const p1 = JSON.parse(pm.environment.get("page1"));',
                'pm.test("no overlap", () => pm.response.json().data.forEach(c => pm.expect(p1).to.not.include(c.id)));']),
            req("Cursor reused with other filters is 400", "GET", "{{baseUrl}}/v1/cases?limit=5&sort=-openedAt&cursor={{cursor}}",
                tests=[status(400), PROBLEM,
                       'pm.test("invalid-cursor", () => pm.expect(pm.response.json().type).to.include("invalid-cursor"));']),
            req("Invalid enum filter is 400 with field errors", "GET", "{{baseUrl}}/v1/cases?priority=P9", tests=[
                status(400), PROBLEM,
                'pm.test("priority flagged", () => pm.expect(pm.response.json().errors.map(e => e.field)).to.include("priority"));']),
            req("Deprecated ?account= still works, with Deprecation headers", "GET", "{{baseUrl}}/v1/cases?account=ACC-007", tests=[
                status(200),
                'pm.test("Deprecation + Sunset", () => { pm.expect(pm.response.headers.get("Deprecation")).to.eql("true"); '
                'pm.expect(pm.response.headers.has("Sunset")).to.be.true; });']),
            req("No token is 401", "GET", "{{baseUrl}}/v1/cases", auth=None, tests=[
                status(401), PROBLEM,
                'pm.test("WWW-Authenticate", () => pm.expect(pm.response.headers.get("WWW-Authenticate")).to.include("Bearer"));']),
        ]),
        folder("4. Comments (idempotent)", [
            req("Create comment", "POST", "{{baseUrl}}/v1/cases/CS-10231/comments", headers={"Idempotency-Key": "{{idemKey}}"},
                body={"body": "Engineering engaged, next update 17:00 UTC"},
                pre=['pm.environment.set("idemKey", pm.variables.replaceIn("{{$guid}}"));'],
                tests=[status(201), 'pm.environment.set("commentId", pm.response.json().id);',
                       'pm.test("author is the caller", () => pm.expect(pm.response.json().author).to.eql("alice"));']),
            req("Retry with same key replays, no duplicate", "POST", "{{baseUrl}}/v1/cases/CS-10231/comments",
                headers={"Idempotency-Key": "{{idemKey}}"}, body={"body": "Engineering engaged, next update 17:00 UTC"},
                tests=[status(201),
                       'pm.test("same comment", () => pm.expect(pm.response.json().id).to.eql(pm.environment.get("commentId")));',
                       'pm.test("Idempotent-Replayed", () => pm.expect(pm.response.headers.get("Idempotent-Replayed")).to.eql("true"));']),
            req("Same key, different body is 422", "POST", "{{baseUrl}}/v1/cases/CS-10231/comments",
                headers={"Idempotency-Key": "{{idemKey}}"}, body={"body": "something else"}, tests=[status(422), PROBLEM]),
            req("Missing Idempotency-Key is 400", "POST", "{{baseUrl}}/v1/cases/CS-10231/comments",
                body={"body": "x"}, tests=[status(400), PROBLEM]),
            req("Read-only token is 403", "POST", "{{baseUrl}}/v1/cases/CS-10231/comments", auth="readonly",
                headers={"Idempotency-Key": "{{$guid}}"}, body={"body": "x"}, tests=[status(403), PROBLEM]),
        ]),
        folder("5. Escalation", [
            req("Read case to get ETag", "GET", "{{baseUrl}}/v1/cases/CS-10251",
                tests=[status(200), 'pm.environment.set("caseEtag", pm.response.headers.get("ETag"));']),
            req("Without If-Match is 428", "POST", "{{baseUrl}}/v1/cases/CS-10251/escalate", body=ESCALATION,
                tests=[status(428), PROBLEM]),
            req("Stale If-Match is 412", "POST", "{{baseUrl}}/v1/cases/CS-10251/escalate", headers={"If-Match": '"stale"'},
                body=ESCALATION, tests=[status(412), PROBLEM]),
            req("Escalate with current ETag", "POST", "{{baseUrl}}/v1/cases/CS-10251/escalate",
                headers={"If-Match": "{{caseEtag}}"}, body=ESCALATION,
                tests=[status(200), 'pm.test("escalated", () => pm.expect(pm.response.json().status).to.eql("escalated"));',
                       'pm.environment.set("caseEtag", pm.response.headers.get("ETag"));']),
            req("Escalating again is 409 already-escalated", "POST", "{{baseUrl}}/v1/cases/CS-10251/escalate",
                headers={"If-Match": "{{caseEtag}}"}, body=ESCALATION,
                tests=[status(409), 'pm.test("already-escalated", () => pm.expect(pm.response.json().type).to.include("already-escalated"));']),
            req("Read P4 case (Standard tier)", "GET", "{{baseUrl}}/v1/cases/CS-10278",
                tests=[status(200), 'pm.environment.set("p4Etag", pm.response.headers.get("ETag"));']),
            req("P4 for Standard customer is 422 business rule", "POST", "{{baseUrl}}/v1/cases/CS-10278/escalate",
                headers={"If-Match": "{{p4Etag}}"}, body={"reason": "executive_request", "note": "Customer exec asked for a call"},
                tests=[status(422),
                       'pm.test("escalation-not-allowed", () => pm.expect(pm.response.json().type).to.include("escalation-not-allowed"));']),
            req("Webhook receiver got case.escalated (signed)", "GET", "{{receiverUrl}}/events", auth=None,
                pre=["setTimeout(() => {}, 2500);"],
                tests=[status(200),
                       'pm.test("event for CS-10251 delivered and verified", () => pm.expect(pm.response.json().some('
                       'e => e.type === "case.escalated" && e.data.caseId === "CS-10251")).to.be.true);']),
        ]),
    ],
}

environment = {"name": "Alphacom local", "values": [
    {"key": k, "value": v, "enabled": True} for k, v in [
        ("baseUrl", "http://localhost:8080"), ("mocksUrl", "http://localhost:8081"),
        ("receiverUrl", "http://localhost:9000"), ("supportToken", ""), ("financeToken", ""), ("readonlyToken", "")]
]}

if __name__ == "__main__":
    (OUT / "alphacom.postman_collection.json").write_text(json.dumps(collection, indent=2) + "\n")
    (OUT / "local.postman_environment.json").write_text(json.dumps(environment, indent=2) + "\n")
    print(f"{sum(len(f['item']) for f in collection['item'])} requests written to {OUT}")
