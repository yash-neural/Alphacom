"""Stand-ins for the three upstream systems, served from one process.

  /casesys/*     case system (Salesforce-like REST, JSON, inconsistent field casing kept on purpose)
  /entitlement/* entitlement system (nightly CSV export + XML change feed)
  /warehouse/*   usage warehouse (Snowflake-style SQL statements API over SQLite)
  /__faults      fault injection used by tests, Postman and the demo

Fixture dates are written relative to 2026-11-25. On startup they are shifted by
whole days so "open for more than 48h" and "renewal in 90 days" still mean the
same thing whenever this is run. Set MOCK_REBASE_DATES=false to keep them as-is.
"""

import asyncio
import copy
import json
import os
import sqlite3
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

DATA = Path(__file__).parent / "data"
ANCHOR = date(2026, 11, 25)

app = FastAPI(title="Mock upstreams", docs_url=None, redoc_url=None)
_lock = threading.Lock()


def _day_shift() -> timedelta:
    if os.getenv("MOCK_REBASE_DATES", "true").lower() == "false":
        return timedelta(0)
    return datetime.now(UTC).date() - ANCHOR


SHIFT = _day_shift()


def _shift_ts(value: str) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) + SHIFT
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _shift_date(value: str) -> str:
    return (date.fromisoformat(value) + SHIFT).isoformat()


def _load_cases() -> dict[str, dict]:
    raw = json.loads((DATA / "cases.json").read_text())
    cases = {}
    for c in raw:
        c["openedAt"] = _shift_ts(c["openedAt"])
        for cm in c.get("Comments", []):
            cm["at"] = _shift_ts(cm["at"])
        # Salesforce-style system modstamp, used for optimistic concurrency.
        c["SystemModstamp"] = c["openedAt"]
        cases[c["id"]] = c
    return cases


def _load_csv() -> str:
    lines = (DATA / "entitlements.csv").read_text().strip().splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        cols = line.split(",")
        cols[3] = _shift_date(cols[3])
        out.append(",".join(cols))
    return "\n".join(out) + "\n"


def _load_xml() -> str:
    import re

    text = (DATA / "entitlements_feed.xml").read_text()
    return re.sub(
        r"<renewal>(\d{4}-\d{2}-\d{2})</renewal>",
        lambda m: f"<renewal>{_shift_date(m.group(1))}</renewal>",
        text,
    )


def _load_warehouse() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript((DATA / "usage.sql").read_text())
    return conn


CASES = _load_cases()
_SEED_CASES = copy.deepcopy(CASES)
ENTITLEMENT_CSV = _load_csv()
ENTITLEMENT_XML = _load_xml()
WAREHOUSE = _load_warehouse()
COMMENT_EXTERNAL_IDS: dict[str, dict] = {}

# system -> {"mode": "ok"|"error"|"timeout", "latency_ms": int, "fail_count": int}
FAULTS: dict[str, dict] = {}


async def _apply_fault(system: str):
    f = FAULTS.get(system)
    if not f:
        return
    if f.get("latency_ms"):
        await asyncio.sleep(f["latency_ms"] / 1000)
    mode = f.get("mode", "ok")
    if f.get("fail_count") is not None:
        if f["fail_count"] <= 0:
            return
        f["fail_count"] -= 1
    if mode == "error":
        raise HTTPException(503, f"{system} unavailable (injected)")
    if mode == "timeout":
        await asyncio.sleep(30)


@app.post("/__faults")
async def set_fault(body: dict):
    system = body.get("system")
    if system not in {"casesys", "entitlement", "warehouse"}:
        raise HTTPException(400, "system must be casesys|entitlement|warehouse")
    if body.get("mode", "ok") == "ok" and not body.get("latency_ms"):
        FAULTS.pop(system, None)
    else:
        FAULTS[system] = {k: body.get(k) for k in ("mode", "latency_ms", "fail_count")}
    return {"faults": FAULTS}


@app.post("/__reset")
async def reset():
    global CASES
    FAULTS.clear()
    COMMENT_EXTERNAL_IDS.clear()
    CASES = copy.deepcopy(_SEED_CASES)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True}


# ---- case system -----------------------------------------------------------

def _status_of(c: dict) -> str:
    return c.get("Status") or c.get("status")


@app.get("/casesys/cases")
async def list_cases(status: str | None = None, priority: str | None = None, accountId: str | None = None):
    await _apply_fault("casesys")
    rows = list(CASES.values())
    if status:
        wanted = set(status.split(","))
        rows = [c for c in rows if _status_of(c) in wanted]
    if priority:
        wanted = set(priority.split(","))
        rows = [c for c in rows if c["priority"] in wanted]
    if accountId:
        wanted = set(accountId.split(","))
        rows = [c for c in rows if c["accountId"] in wanted]
    return {"totalSize": len(rows), "records": rows}


@app.get("/casesys/cases/{case_id}")
async def get_case(case_id: str):
    await _apply_fault("casesys")
    c = CASES.get(case_id)
    if not c:
        raise HTTPException(404, "case not found")
    return c


@app.post("/casesys/cases/{case_id}/comments", status_code=201)
async def add_comment(case_id: str, body: dict):
    await _apply_fault("casesys")
    with _lock:
        c = CASES.get(case_id)
        if not c:
            raise HTTPException(404, "case not found")
        ext = body.get("externalId")
        if ext and ext in COMMENT_EXTERNAL_IDS:
            return JSONResponse(COMMENT_EXTERNAL_IDS[ext], status_code=200)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        comments = c.setdefault("Comments", [])
        comment = {
            "id": f"{case_id}-C{len(comments) + 1}",
            "author": body["author"],
            "body": body["body"],
            "at": now,
        }
        if body.get("via"):
            comment["via"] = body["via"]
        comments.append(comment)
        c["SystemModstamp"] = now
        if ext:
            COMMENT_EXTERNAL_IDS[ext] = comment
        return comment


@app.patch("/casesys/cases/{case_id}")
async def update_case(case_id: str, body: dict):
    await _apply_fault("casesys")
    with _lock:
        c = CASES.get(case_id)
        if not c:
            raise HTTPException(404, "case not found")
        expected = body.pop("expectedModstamp", None)
        if expected and expected != c["SystemModstamp"]:
            raise HTTPException(409, "record was modified by someone else")
        if "status" in body:
            c.pop("status", None)
            c["Status"] = body.pop("status")
        c.update(body)
        # Second resolution keeps back-to-back writes distinguishable.
        c["SystemModstamp"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return c


# ---- entitlement system ------------------------------------------------------

@app.get("/entitlement/export/accounts.csv")
async def entitlement_csv():
    await _apply_fault("entitlement")
    return PlainTextResponse(ENTITLEMENT_CSV, media_type="text/csv")


@app.get("/entitlement/feeds/accounts.xml")
async def entitlement_xml():
    await _apply_fault("entitlement")
    return Response(ENTITLEMENT_XML, media_type="application/xml")


# ---- usage warehouse ---------------------------------------------------------

@app.post("/warehouse/api/v2/statements")
async def run_statement(request: Request):
    await _apply_fault("warehouse")
    body = await request.json()
    stmt = body.get("statement", "")
    if not stmt.lstrip().lower().startswith("select"):
        raise HTTPException(400, "read-only warehouse: SELECT only")
    bindings = [b["value"] for b in body.get("bindings", [])]
    with _lock:
        cur = WAREHOUSE.execute(stmt, bindings)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return {
        "resultSetMetaData": {"rowType": [{"name": c} for c in cols]},
        "data": [[str(v) for v in r] for r in rows],
    }
