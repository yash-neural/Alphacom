# Customer Entitlement & Support Case API

One API in front of three systems (a Salesforce-like case system, a NetSuite-like
entitlement system, a Snowflake-like usage warehouse), plus an MCP server so a
Support Copilot can use it safely on behalf of a signed-in engineer.

```
 MuleSoft / apps ──REST──┐
                         ▼
 LLM ──MCP──► mcp/server.py ──REST (delegated token)──► API :8080 ──► case system   (JSON)
              guardrails,                               │  FastAPI    entitlements (CSV + XML)
              confirmation                              │             usage        (SQL)
                                                        ├──► Postgres: idempotency, audit, webhooks outbox
                                                        └──► case.escalated webhook ──► receiver :9000
```

The three upstreams are mocked in one process (`mocks/`, port 8081) with a
fault-injection endpoint so failure handling can be demonstrated.

## Run it (about 2 minutes)

Needs Docker and [uv](https://docs.astral.sh/uv/).

```bash
docker compose up -d --build --wait      # or: make run
```

* API docs (the hand-written contract): http://localhost:8080/docs
* Readiness and circuit-breaker state: http://localhost:8080/ready

Try the sample question as a REST call:

```bash
TOKEN=$(curl -s -X POST localhost:8080/dev/token -H 'content-type: application/json' \
  -d '{"sub":"alice","scopes":["customers:read","cases:read","cases:write","cases:escalate"]}' | jq -r .access_token)

curl -s "localhost:8080/v1/cases?priority=P1&status=open,in_progress,pending_customer,escalated&customerTier=Platinum\
&openedBefore=$(date -u -d '-48 hours' +%FT%TZ)&renewalBefore=$(date -u -d '+90 days' +%F)" \
  -H "Authorization: Bearer $TOKEN" | jq '.data[] | {id, accountId, renewal: .customer.renewalDate}'
```

Expected: CS-10231, CS-10240 and CS-10251. Several seeded cases are near-misses
on purpose (see "Seed data" below).

`/dev/token` stands in for the corporate IdP and only exists when `APP_ENV=dev`.

## Tests

```bash
make test        # or: docker compose up -d --wait postgres && uv run pytest -q
```

| Suite | What it covers |
|---|---|
| `tests/unit` | id canonicalisation, CSV/XML merge, retries + circuit breaker, cursors, rate limiter, webhook signatures, SSRF guard |
| `tests/integration` | every endpoint end to end against real Postgres (run through the Alembic migration, connecting as the restricted DB role): auth, field-level ARR, degraded mode, stale cache, pagination, idempotency, escalation rules, webhooks, audit immutability |
| `tests/contract` | real responses validated against `openapi.yaml` with openapi-core, including error responses |
| `tests/mcp` | MCP guardrails without an LLM: delegation, confirmation, injection marking, PII redaction, output caps, rate limits, audit |

Postman: import `postman/alphacom.postman_collection.json` + `postman/local.postman_environment.json`,
or `make postman` (newman). Run the folders in order; folder 0 resets the mock data and gets tokens.

## The copilot (MCP)

Tools: `search_cases`, `get_customer_summary`, `get_case`, `add_case_comment`, `escalate_case`.

**With an LLM script (OpenAI):**

```bash
cp .env.example .env          # put OPENAI_API_KEY in it
uv run --group demo python mcp/demo.py --follow-up
```

It asks the sample question, then asks the copilot to escalate the Acme case. The
escalation stops and asks *you* to approve on the terminal. A markdown transcript is
written to `mcp/transcripts/`.

**With Claude Code / Claude Desktop:** `.mcp.json` in the repo root registers the
server for Claude Code. For Claude Desktop add the same block to its config,
using an absolute path to `mcp/server.py`.

**Evals:** `uv run --group demo python mcp/evals/run_llm_evals.py --repeat 3`. See [TESTEVALS.md](TESTEVALS.md).

## Seed data

Dates in the fixtures are relative to 2026-11-25 and shifted to "today" when the mocks
start, so the sample question keeps working. Deliberate traps in the data:

| Case | Why it is *not* in the sample answer |
|---|---|
| CS-10247 | P1 Platinum, but opened less than 48h ago |
| CS-10260 | Halden's CSV says renewal 2026-12-20, but the newer XML feed says they renewed (2027-12-20) |
| CS-10262 | renewal more than 90 days away |
| CS-10266 | Gold, not Platinum |
| CS-10288 | account ACC-099 has no entitlement record |

CS-10240 also contains a prompt-injection comment and a customer phone number and
email, which the MCP layer redacts.

Reset the mock data at any time: `curl -X POST localhost:8081/__reset`.
Inject failures: `curl -X POST localhost:8081/__faults -H 'content-type: application/json' -d '{"system":"warehouse","mode":"error"}'`
(`system`: casesys | entitlement | warehouse; `mode`: error | timeout; optional `latency_ms`, `fail_count`).

## Repo map

| Path | |
|---|---|
| `openapi.yaml` | the contract (Part A) |
| `src/app/` | API: `routes/`, `upstreams.py` (normalisation, resilience), `idempotency.py`, `webhooks.py` (outbox + signing), `auth.py`, `models.py` |
| `migrations/` | Alembic migration (tables, indexes, append-only trigger, DB grants) |
| `mocks/` | mock upstreams + fixture data (JSON, CSV, XML, SQL) |
| `examples/webhook_receiver.py` | reference receiver that verifies signatures |
| `mcp/` | MCP server, guardrails, demo, LLM evals |
| `tests/` | unit, integration, contract, MCP guardrail tests |
| `postman/` | collection, environment, and the script that generates them |

## Troubleshooting

* **Windows, `docker compose build` fails with "additional privileges requested: fs.read"**: open
  the folder as `C:\...` (upper-case drive letter). Compose's bake builder compares paths
  case-sensitively.
* **Port 55432 in use**: change the host port in `docker-compose.yml` and `TEST_PG_HOST`.
* **Circuit open after fault-injection**: wait 30s or restart the API; `/ready` shows breaker state.
