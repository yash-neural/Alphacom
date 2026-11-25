"""Bearer-token auth, scope checks, per-principal rate limiting, and a dev-only token issuer."""

import time
from dataclasses import dataclass

import jwt
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.problems import ApiProblem

ALL_SCOPES = {
    "customers:read", "customers:financials",
    "cases:read", "cases:write", "cases:escalate",
    "webhooks:manage",
}

# The most an agent can ever hold, regardless of who it acts for. Financial fields are
# deliberately excluded: no LLM sees ARR until there is a reviewed use case for it.
AGENT_CEILINGS = {
    "support-copilot": {"customers:read", "cases:read", "cases:write", "cases:escalate"},
}


@dataclass(frozen=True)
class Principal:
    sub: str
    scopes: frozenset[str]
    client_id: str
    actor: str | None = None  # set when an agent acts on the user's behalf

    @property
    def rate_key(self) -> str:
        return f"agent:{self.actor}:{self.sub}" if self.actor else f"client:{self.client_id}"

    @property
    def idempotency_scope(self) -> str:
        return f"{self.sub}|{self.client_id}"


def _unauthorized(detail: str) -> ApiProblem:
    return ApiProblem(401, "unauthorized", "Unauthorized", detail,
                      headers={"WWW-Authenticate": 'Bearer realm="alphacom-api"'})


def decode_token(token: str, settings) -> Principal:
    try:
        claims = jwt.decode(
            token, settings.jwt_secret, algorithms=["HS256"],
            audience=settings.jwt_audience, issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "iss", "aud"]}, leeway=30,
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("token expired")
    except jwt.InvalidTokenError as e:
        raise _unauthorized(f"invalid token: {e}")
    scopes = frozenset(str(claims.get("scope", "")).split())
    actor = (claims.get("act") or {}).get("sub")
    return Principal(sub=claims["sub"], scopes=scopes, client_id=claims.get("client_id", "unknown"), actor=actor)


def authenticate(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized("missing bearer token")
    principal = decode_token(token, request.app.state.settings)
    request.state.principal = principal
    return principal


def require(*scopes: str):
    """Dependency: authenticate, check scopes, then apply the per-principal rate limit."""

    def dep(request: Request, principal: Principal = Depends(authenticate)) -> Principal:
        missing = [s for s in scopes if s not in principal.scopes]
        if missing:
            request.state.audit_outcome = "denied"
            raise ApiProblem(403, "insufficient-scope", "Insufficient scope",
                             f"This operation requires scope: {' '.join(missing)}")
        s = request.app.state.settings
        limit = s.agent_rate_limit_per_min if principal.actor else s.rate_limit_per_min
        decision = request.app.state.ratelimiter.check(principal.rate_key, limit, 60)
        request.state.ratelimit = decision
        if not decision.allowed:
            raise ApiProblem(429, "rate-limited", "Too many requests",
                             f"Limit is {limit} requests per minute for this client",
                             headers={"Retry-After": str(max(decision.reset_s, 1))})
        return principal

    return dep


# ---- dev token issuer ---------------------------------------------------------
# Stands in for the corporate IdP so the API can be exercised locally. Only mounted
# when APP_ENV=dev.

dev_router = APIRouter(prefix="/dev", tags=["dev"], include_in_schema=False)


class DevTokenRequest(BaseModel):
    sub: str = Field(min_length=1, max_length=64)
    scopes: list[str]
    client_id: str = "dev-client"
    ttl_s: int = Field(3600, ge=60, le=86400)


class TokenExchangeRequest(BaseModel):
    subject_token: str
    actor: str
    scopes: list[str] | None = None


def _mint(settings, sub: str, scopes: set[str], client_id: str, ttl_s: int, act: dict | None = None) -> str:
    now = int(time.time())
    claims = {
        "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "sub": sub,
        "client_id": client_id, "scope": " ".join(sorted(scopes)),
        "iat": now, "nbf": now, "exp": now + ttl_s,
    }
    if act:
        claims["act"] = act
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


@dev_router.post("/token")
def issue_dev_token(req: DevTokenRequest, request: Request):
    unknown = set(req.scopes) - ALL_SCOPES
    if unknown:
        raise ApiProblem(400, "validation-error", "Unknown scopes", ", ".join(sorted(unknown)))
    settings = request.app.state.settings
    token = _mint(settings, req.sub, set(req.scopes), req.client_id, req.ttl_s)
    return {"access_token": token, "token_type": "Bearer", "expires_in": req.ttl_s}


@dev_router.post("/token/exchange")
def exchange_token(req: TokenExchangeRequest, request: Request):
    """RFC 8693-style token exchange: user token in, short-lived delegated token out.

    Resulting scopes = requested ∩ user's scopes ∩ agent ceiling. The agent can never
    do more than the user, and never more than its ceiling.
    """
    settings = request.app.state.settings
    user = decode_token(req.subject_token, settings)
    if user.actor:
        raise ApiProblem(400, "invalid-grant", "Invalid grant", "cannot re-delegate a delegated token")
    ceiling = AGENT_CEILINGS.get(req.actor)
    if ceiling is None:
        raise ApiProblem(400, "invalid-grant", "Invalid grant", f"unknown actor {req.actor!r}")
    wanted = set(req.scopes) if req.scopes else set(ceiling)
    granted = wanted & set(user.scopes) & ceiling
    ttl = 900
    token = _mint(settings, user.sub, granted, req.actor, ttl, act={"sub": req.actor})
    return {
        "access_token": token, "token_type": "Bearer", "expires_in": ttl,
        "scope": " ".join(sorted(granted)),
        "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
    }
