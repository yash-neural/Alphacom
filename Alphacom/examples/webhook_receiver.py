"""Sample case.escalated receiver: verifies the signature, dedupes on Webhook-Id.

Run: uvicorn examples.webhook_receiver:app --port 9000
Secrets come from WEBHOOK_SECRETS (comma separated, so you can rotate).
GET /events shows what was received.
"""

import json
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from app.webhooks import verify  # noqa: E402

logging.basicConfig(level="INFO", format="%(message)s")
logger = logging.getLogger("receiver")

SECRETS = [s for s in os.getenv("WEBHOOK_SECRETS", "whsec_local_demo_secret").split(",") if s]
app = FastAPI(title="Webhook receiver")
seen: dict[str, dict] = {}


@app.post("/hooks")
async def receive(request: Request):
    raw = await request.body()  # verify on the raw bytes, before parsing
    h = request.headers
    msg_id, ts, sig = h.get("webhook-id", ""), h.get("webhook-timestamp", ""), h.get("webhook-signature", "")
    if not verify(SECRETS, msg_id, ts, sig, raw):
        logger.warning(json.dumps({"msg": "rejected webhook: bad signature or stale timestamp", "id": msg_id}))
        raise HTTPException(401, "invalid signature")
    if msg_id in seen:
        return {"status": "duplicate ignored"}
    event = json.loads(raw)
    seen[msg_id] = event
    logger.info(json.dumps({"msg": "received", "id": msg_id, "type": event["type"], "data": event["data"]}))
    return {"status": "ok"}


@app.get("/events")
async def events():
    return list(seen.values())
