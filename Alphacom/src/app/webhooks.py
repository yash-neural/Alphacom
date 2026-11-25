"""Webhook subscriptions, signing, and an outbox-based dispatcher.

Events are written to webhook_outbox (one row per subscription) and a background
task delivers them. The outbox doubles as a simple local queue: it survives restarts,
retries with backoff, and dead-letters after the last attempt.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
import uuid
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from app.auth import Principal, require
from app.db import Database, iso, utcnow
from app.logs import log
from app.models import WebhookDelivery, WebhookSubscription
from app.problems import ApiProblem, not_found

logger = logging.getLogger("app.webhooks")

# Seconds to wait before attempt n+1. Six attempts over roughly 45 minutes, then dead.
BACKOFF_S = [5, 30, 120, 600, 1800]
DELIVERY_TIMEOUT_S = 5.0
SIGNATURE_TOLERANCE_S = 300
LEASE_S = 60  # a claimed delivery is invisible to other dispatchers for this long


# ---- signing (shared by sender and receivers) ----------------------------------------

def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256)
    return "v1=" + mac.hexdigest()


def verify(secrets_: list[str], msg_id: str, timestamp: str, signature_header: str, body: bytes,
           *, now: float | None = None, tolerance_s: int = SIGNATURE_TOLERANCE_S) -> bool:
    """Receiver-side check. Accepts any of several secrets (rotation)."""
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > tolerance_s:
        return False
    sent = [p for p in signature_header.split() if p.startswith("v1=")]
    for secret in secrets_:
        expected = sign(secret, msg_id, ts, body)
        if any(hmac.compare_digest(expected, s) for s in sent):
            return True
    return False


# ---- SSRF guard ------------------------------------------------------------------------

def validate_target_url(url: str, allowed_hosts: tuple[str, ...], is_dev: bool) -> None:
    p = urlparse(url)
    if p.scheme not in ("https", "http") or not p.hostname:
        raise ApiProblem(400, "validation-error", "Invalid webhook URL", "URL must be absolute http(s)")
    if is_dev and p.hostname in allowed_hosts:
        return
    if p.scheme != "https":
        raise ApiProblem(400, "validation-error", "Invalid webhook URL", "URL must use https")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or 443)
    except socket.gaierror:
        raise ApiProblem(400, "validation-error", "Invalid webhook URL", "host does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ApiProblem(400, "validation-error", "Invalid webhook URL", "URL resolves to a non-public address")
    # The dispatcher should re-check at send time to defeat DNS rebinding (see SECURITY.md).


# ---- outbox -----------------------------------------------------------------------------

async def enqueue(db: Database, event_type: str, data: dict, correlation_id: str) -> str | None:
    async with db.session() as s, s.begin():
        sub_ids = (await s.scalars(
            select(WebhookSubscription.id)
            .where(WebhookSubscription.active, WebhookSubscription.events.contains([event_type]))
        )).all()
        if not sub_ids:
            return None
        event_id = f"evt_{uuid.uuid4().hex}"
        payload = json.dumps({"id": event_id, "type": event_type, "occurredAt": iso(utcnow()),
                              "correlationId": correlation_id, "data": data})
        s.add_all(WebhookDelivery(id=f"dlv_{uuid.uuid4().hex}", event_id=event_id, subscription_id=sid,
                                  event_type=event_type, payload=payload) for sid in sub_ids)
        return event_id


async def _claim(db: Database, limit: int) -> list[tuple[WebhookDelivery, WebhookSubscription]]:
    """Lease due deliveries. SKIP LOCKED means several API replicas can run the
    dispatcher without ever picking up the same row."""
    async with db.session() as s, s.begin():
        rows = (await s.execute(
            select(WebhookDelivery, WebhookSubscription)
            .join(WebhookSubscription, WebhookSubscription.id == WebhookDelivery.subscription_id)
            .where(WebhookDelivery.status == "pending", WebhookDelivery.next_attempt_at <= utcnow())
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit)
            .with_for_update(of=WebhookDelivery, skip_locked=True)
        )).all()
        for delivery, _ in rows:
            delivery.next_attempt_at = utcnow() + timedelta(seconds=LEASE_S)
        return [(d, sub) for d, sub in rows]


async def dispatch_due(db: Database, http: httpx.AsyncClient, *, limit: int = 20) -> int:
    claimed = await _claim(db, limit)
    for d, sub in claimed:
        body = d.payload.encode()
        ts = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "Webhook-Id": d.event_id,
            "Webhook-Timestamp": str(ts),
            "Webhook-Signature": sign(sub.secret, d.event_id, ts, body),
            "User-Agent": "alphacom-webhooks/1",
        }
        error = None
        if not sub.active:
            error = "subscription inactive"
        else:
            try:
                resp = await http.post(sub.url, content=body, headers=headers, timeout=DELIVERY_TIMEOUT_S,
                                       follow_redirects=False)
                if not 200 <= resp.status_code < 300:
                    error = f"HTTP {resp.status_code}"
            except httpx.HTTPError as e:
                error = type(e).__name__

        async with db.session() as s, s.begin():
            row = await s.get(WebhookDelivery, d.id)
            row.attempts += 1
            row.last_error = error
            if error is None:
                row.status, row.delivered_at = "delivered", utcnow()
                log(logger, logging.INFO, "webhook delivered", event_id=d.event_id, attempts=row.attempts)
            elif row.attempts > len(BACKOFF_S):
                row.status = "dead"
                log(logger, logging.ERROR, "webhook dead-lettered", event_id=d.event_id, error=error)
            else:
                row.next_attempt_at = utcnow() + timedelta(seconds=BACKOFF_S[row.attempts - 1])
                log(logger, logging.WARNING, "webhook delivery failed", event_id=d.event_id,
                    attempts=row.attempts, error=error)
    return len(claimed)


async def run_dispatcher(app, interval_s: float = 1.0) -> None:
    ticks = 0
    while True:
        try:
            await dispatch_due(app.state.db, app.state.http)
            ticks += 1
            if ticks % 300 == 0:
                await app.state.idempotency.sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("dispatcher loop error")
        await asyncio.sleep(interval_s)


def _public(sub: WebhookSubscription) -> dict:
    return {"id": sub.id, "url": sub.url, "events": list(sub.events), "active": sub.active,
            "createdAt": iso(sub.created_at)}


async def create_subscription(db: Database, url: str, events: list[str], created_by: str,
                              secret: str | None = None) -> dict:
    sub = WebhookSubscription(
        id=f"whs_{uuid.uuid4().hex[:16]}", url=url, events=events, active=True,
        secret=secret or "whsec_" + secrets.token_urlsafe(32), created_by=created_by, created_at=utcnow(),
    )
    async with db.session() as s, s.begin():
        s.add(sub)
    return {**_public(sub), "secret": sub.secret}


# ---- subscription API -------------------------------------------------------------

router = APIRouter(tags=["Webhooks"])


class NewSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2048)
    events: list[str] = Field(min_length=1)


@router.post("/v1/webhook-subscriptions", status_code=201)
async def create(body: NewSubscription, request: Request, principal: Principal = Depends(require("webhooks:manage"))):
    bad = set(body.events) - {"case.escalated"}
    if bad:
        raise ApiProblem(400, "validation-error", "Unknown event type", ", ".join(sorted(bad)))
    s = request.app.state.settings
    validate_target_url(body.url, s.webhook_allowed_hosts, s.is_dev)
    return await create_subscription(request.app.state.db, body.url, body.events, principal.sub)


@router.get("/v1/webhook-subscriptions")
async def list_subs(request: Request, _: Principal = Depends(require("webhooks:manage"))):
    async with request.app.state.db.session() as s:
        subs = (await s.scalars(select(WebhookSubscription).order_by(WebhookSubscription.created_at))).all()
    return {"data": [_public(x) for x in subs]}


@router.delete("/v1/webhook-subscriptions/{subscriptionId}", status_code=204)
async def delete_sub(subscriptionId: str, request: Request, _: Principal = Depends(require("webhooks:manage"))):
    async with request.app.state.db.session() as s, s.begin():
        result = await s.execute(delete(WebhookSubscription).where(WebhookSubscription.id == subscriptionId))
    if not result.rowcount:
        raise not_found(f"Subscription {subscriptionId}")
    return Response(status_code=204)
