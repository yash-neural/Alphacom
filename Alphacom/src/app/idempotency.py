"""Idempotency-Key handling for POSTs.

begin() claims the key with INSERT ... ON CONFLICT DO NOTHING (atomic, even across
replicas) or returns the stored response for a replay. The handler does its work,
then complete() stores the response or release() frees the key so a retry can run
again. Keys are scoped per caller and kept for 24h.
"""

import hashlib
import json
from datetime import timedelta

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert

from app.db import Database, utcnow
from app.models import IdempotencyKey
from app.problems import ApiProblem

TTL = timedelta(hours=24)
# A key stuck in_progress this long belongs to a crashed request; let a retry take it.
STALE_IN_PROGRESS = timedelta(seconds=60)


def request_hash(path: str, body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{path}\n{canonical}".encode()).hexdigest()


class Replay:
    def __init__(self, status: int, body: dict, headers: dict):
        self.status, self.body, self.headers = status, body, headers


def _in_flight() -> ApiProblem:
    return ApiProblem(409, "idempotency-in-progress", "Request in progress",
                      "A request with this Idempotency-Key is still being processed; retry shortly",
                      headers={"Retry-After": "2"})


class IdempotencyStore:
    def __init__(self, db: Database):
        self.db = db

    async def begin(self, scope: str, key: str, method: str, path: str, req_hash: str) -> Replay | None:
        now = utcnow()
        fresh = dict(method=method, path=path, request_hash=req_hash, state="in_progress",
                     response_status=None, response_body=None, response_headers=None,
                     created_at=now, expires_at=now + TTL)
        async with self.db.session() as s, s.begin():
            claimed = await s.execute(
                insert(IdempotencyKey).values(scope=scope, key=key, **fresh)
                .on_conflict_do_nothing().returning(IdempotencyKey.key)
            )
            if claimed.first():
                return None
            row = await s.get(IdempotencyKey, (scope, key), with_for_update=True)
            if row is None:
                raise _in_flight()  # deleted between our insert and select; caller retries
            if row.expires_at <= now:
                for k, v in fresh.items():
                    setattr(row, k, v)
                return None
            if row.request_hash != req_hash:
                raise ApiProblem(422, "idempotency-key-reused", "Idempotency key reused",
                                 "This Idempotency-Key was already used with a different request")
            if row.state == "completed":
                return Replay(row.response_status, row.response_body, row.response_headers or {})
            if row.created_at > now - STALE_IN_PROGRESS:
                raise _in_flight()
            row.created_at = now  # take over a crashed request's claim (row is locked)
            return None

    async def complete(self, scope: str, key: str, status: int, body: dict, headers: dict | None = None) -> None:
        async with self.db.session() as s, s.begin():
            await s.execute(
                update(IdempotencyKey)
                .where(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
                .values(state="completed", response_status=status, response_body=body, response_headers=headers or {})
            )

    async def release(self, scope: str, key: str) -> None:
        async with self.db.session() as s, s.begin():
            await s.execute(delete(IdempotencyKey).where(
                IdempotencyKey.scope == scope, IdempotencyKey.key == key, IdempotencyKey.state == "in_progress"))

    async def sweep(self) -> int:
        async with self.db.session() as s, s.begin():
            result = await s.execute(delete(IdempotencyKey).where(IdempotencyKey.expires_at <= utcnow()))
            return result.rowcount
