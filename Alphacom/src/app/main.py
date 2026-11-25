import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select

from app import audit, problems, webhooks
from app.auth import dev_router
from app.config import Settings
from app.idempotency import IdempotencyStore
from app.logs import correlation_id, log, setup_logging
from app.models import WebhookSubscription
from app.problems import ApiProblem, problem_response
from app.ratelimit import RateLimiter
from app.routes import cases, customers
from app.db import Database
from app.upstreams import Upstreams

logger = logging.getLogger("app")
SPEC = Path(__file__).resolve().parents[2] / "openapi.yaml"


async def _subscription_exists(db: Database, url: str) -> bool:
    async with db.session() as s:
        return (await s.scalar(select(WebhookSubscription.id).where(WebhookSubscription.url == url))) is not None


@asynccontextmanager
async def lifespan(app: FastAPI):
    s: Settings = app.state.settings
    db = Database(s.database_url)
    http = httpx.AsyncClient(limits=httpx.Limits(max_connections=50), mounts=app.state.http_mounts)
    app.state.db = db
    app.state.http = http
    app.state.upstreams = Upstreams(s, http)
    app.state.idempotency = IdempotencyStore(db)
    app.state.ratelimiter = RateLimiter()

    if s.seed_webhook_url and not await _subscription_exists(db, s.seed_webhook_url):
        await webhooks.create_subscription(db, s.seed_webhook_url, ["case.escalated"], "bootstrap",
                                           secret=s.seed_webhook_secret or None)
        log(logger, logging.INFO, "seeded webhook subscription", url=s.seed_webhook_url)

    task = asyncio.create_task(webhooks.run_dispatcher(app)) if s.dispatcher_enabled else None
    log(logger, logging.INFO, "api started", env=s.env)
    try:
        yield
    finally:
        if task:
            task.cancel()
        await http.aclose()
        await db.close()


def create_app(settings: Settings | None = None, http_mounts: dict | None = None) -> FastAPI:
    """http_mounts lets tests route upstream and webhook calls to in-process ASGI apps."""
    settings = settings or Settings()
    setup_logging()
    # We serve the hand-written contract, not a generated one: the spec is the source of truth.
    app = FastAPI(title="Customer Entitlement & Support Case API", lifespan=lifespan,
                  openapi_url=None, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.http_mounts = http_mounts
    problems.install(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        cid = request.headers.get("x-correlation-id", "")[:128] or str(uuid.uuid4())
        token = correlation_id.set(cid)
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                logger.exception("unhandled error")
                response = problem_response(ApiProblem(500, "internal-error", "Internal error",
                                                       "Something went wrong; quote the correlation id"),
                                            request.url.path)
            response.headers["X-Correlation-ID"] = cid
            rl = getattr(request.state, "ratelimit", None)
            if rl:
                response.headers["RateLimit-Limit"] = str(rl.limit)
                response.headers["RateLimit-Remaining"] = str(rl.remaining)
                response.headers["RateLimit-Reset"] = str(rl.reset_s)

            principal = getattr(request.state, "principal", None)
            route = request.scope.get("route")
            quiet = request.url.path in ("/health", "/ready")
            log(logger, logging.DEBUG if quiet else logging.INFO, "request", method=request.method, path=request.url.path,
                status=response.status_code, ms=round((time.perf_counter() - started) * 1000, 1),
                sub=getattr(principal, "sub", None), actor=getattr(principal, "actor", None))

            if request.url.path.startswith("/v1/"):
                status = response.status_code
                outcome = "success" if status < 400 else "denied" if status in (401, 403) else "error"
                try:
                    await audit.record(
                        request.app.state.db, correlation_id=cid, principal=principal,
                        action=f"{request.method} {getattr(route, 'path', request.url.path)}",
                        resource=getattr(request.state, "resource", None),
                        outcome=outcome, http_status=status,
                    )
                except Exception:
                    # Audit failure is loud but doesn't turn a completed write into a 500.
                    logger.exception("audit write failed")
            return response
        finally:
            correlation_id.reset(token)

    app.include_router(customers.router)
    app.include_router(cases.router)
    app.include_router(webhooks.router)
    if settings.is_dev:
        app.include_router(dev_router)

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    @app.get("/ready", include_in_schema=False)
    async def ready(request: Request):
        try:
            await request.app.state.db.ping()
        except Exception:
            return JSONResponse({"status": "not ready", "database": "down"}, status_code=503)
        return {"status": "ready", "upstreamCircuits": {
            name: b.state for name, b in request.app.state.upstreams.breakers.items()}}

    @app.get("/openapi.yaml", include_in_schema=False)
    async def spec():
        return FileResponse(SPEC, media_type="application/yaml")

    @app.get("/docs", include_in_schema=False)
    async def docs():
        return get_swagger_ui_html(openapi_url="/openapi.yaml", title="Alphacom API docs")

    return app


app = create_app()
