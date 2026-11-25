"""Test harness: the API, the mock upstreams and the sample webhook receiver all run
in-process (httpx ASGI transports), against a real Postgres.

Postgres comes from `docker compose up -d postgres`. The session creates a fresh
`alphacom_test` database, runs the Alembic migrations on it (so the migration itself
is under test), and the app connects as the restricted `api` role, exactly as in
docker compose. Tables are truncated between tests.
"""

import asyncio
import os

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

import mocks.app as mock_upstreams
from app.config import Settings
from app.main import create_app
from examples import webhook_receiver

PG_HOST = os.getenv("TEST_PG_HOST", "localhost:55432")
TEST_DB = "alphacom_test"
OWNER_DSN = f"postgresql://postgres:postgres-dev-password@{PG_HOST}"
APP_URL = f"postgresql+asyncpg://api:api-dev-password@{PG_HOST}/{TEST_DB}"
TABLES = "webhook_outbox, webhook_subscriptions, idempotency_keys, audit_log"


async def _recreate_test_db() -> None:
    conn = await asyncpg.connect(f"{OWNER_DSN}/postgres", timeout=3)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {TEST_DB}")
    finally:
        await conn.close()


def _setup_database() -> bool:
    try:
        asyncio.run(_recreate_test_db())
    except (OSError, asyncpg.PostgresError, asyncio.TimeoutError):
        return False
    os.environ["MIGRATIONS_DATABASE_URL"] = f"postgresql+asyncpg://postgres:postgres-dev-password@{PG_HOST}/{TEST_DB}"
    command.upgrade(AlembicConfig("alembic.ini"), "head")
    return True


PG_UP = _setup_database()


@pytest.fixture
async def clean_db():
    if not PG_UP:
        pytest.skip("Postgres not reachable; run `docker compose up -d postgres`")
    yield
    conn = await asyncpg.connect(f"{OWNER_DSN}/{TEST_DB}")
    try:
        # TRUNCATE doesn't fire the row-level append-only trigger; only the owner can do it.
        await conn.execute(f"TRUNCATE {TABLES}")
    finally:
        await conn.close()


@pytest.fixture
async def upstreams():
    """Direct handle on the mock upstreams, reset to seed data for every test."""
    await mock_upstreams.reset()
    webhook_receiver.seen.clear()
    yield mock_upstreams
    await mock_upstreams.reset()


def make_settings(**overrides) -> Settings:
    base = dict(
        env="dev",
        database_url=APP_URL,
        casesys_url="http://upstreams/casesys",
        entitlement_url="http://upstreams/entitlement",
        warehouse_url="http://upstreams/warehouse",
        upstream_timeout_s=0.5,
        upstream_retries=2,
        optional_source_budget_s=1.0,
        dispatcher_enabled=False,
        webhook_allowed_hosts=("receiver",),
    )
    base.update(overrides)
    return Settings(**base)


MOUNTS = {
    "http://upstreams": httpx.ASGITransport(app=mock_upstreams.app),
    "http://receiver": httpx.ASGITransport(app=webhook_receiver.app),
}


@pytest.fixture
async def make_client(clean_db, upstreams):
    """Factory so a test can tweak settings; most tests just use `client`."""
    stack = []

    async def factory(**overrides):
        app = create_app(make_settings(**overrides), http_mounts=MOUNTS)
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")
        client.app = app
        stack.append((ctx, client))
        return client

    yield factory
    for ctx, client in reversed(stack):
        await client.aclose()
        await ctx.__aexit__(None, None, None)


@pytest.fixture
async def client(make_client):
    return await make_client()


SUPPORT = ["customers:read", "cases:read", "cases:write", "cases:escalate"]
FINANCE = ["customers:read", "customers:financials", "cases:read"]


@pytest.fixture
def token(client):
    async def mint(scopes=SUPPORT, sub="alice", client_id="test-client"):
        r = await client.post("/dev/token", json={"sub": sub, "scopes": scopes, "client_id": client_id})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    return mint


@pytest.fixture
def auth(token):
    async def headers(scopes=SUPPORT, **kw):
        return {"Authorization": f"Bearer {await token(scopes, **kw)}"}

    return headers
