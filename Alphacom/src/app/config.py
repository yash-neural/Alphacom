import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    env: str = field(default_factory=lambda: _env("APP_ENV", "dev"))
    # The app connects as the restricted "api" role; migrations run as the owner (see docker-compose.yml).
    database_url: str = field(default_factory=lambda: _env(
        "DATABASE_URL", "postgresql+asyncpg://api:api-dev-password@localhost:55432/alphacom"))

    casesys_url: str = field(default_factory=lambda: _env("CASESYS_URL", "http://localhost:8081/casesys"))
    entitlement_url: str = field(default_factory=lambda: _env("ENTITLEMENT_URL", "http://localhost:8081/entitlement"))
    warehouse_url: str = field(default_factory=lambda: _env("WAREHOUSE_URL", "http://localhost:8081/warehouse"))

    # Dev only: tokens are HS256 with a shared secret. In any real environment this is
    # RS256/ES256 validated against the IdP's JWKS (see SECURITY.md).
    jwt_secret: str = field(default_factory=lambda: _env("JWT_SECRET", "dev-only-not-a-secret-change-me-0123456789"))
    jwt_issuer: str = field(default_factory=lambda: _env("JWT_ISSUER", "http://localhost:8080/dev-idp"))
    jwt_audience: str = field(default_factory=lambda: _env("JWT_AUDIENCE", "alphacom-api"))
    cursor_secret: str = field(default_factory=lambda: _env("CURSOR_SECRET", "dev-cursor-secret"))

    upstream_timeout_s: float = field(default_factory=lambda: float(_env("UPSTREAM_TIMEOUT_S", "2.0")))
    upstream_retries: int = field(default_factory=lambda: int(_env("UPSTREAM_RETRIES", "3")))
    optional_source_budget_s: float = field(default_factory=lambda: float(_env("OPTIONAL_SOURCE_BUDGET_S", "2.5")))
    entitlement_cache_ttl_s: int = 300
    entitlement_max_stale_s: int = 24 * 3600

    rate_limit_per_min: int = field(default_factory=lambda: int(_env("RATE_LIMIT_PER_MIN", "120")))
    agent_rate_limit_per_min: int = field(default_factory=lambda: int(_env("AGENT_RATE_LIMIT_PER_MIN", "60")))
    escalations_per_hour: int = field(default_factory=lambda: int(_env("ESCALATIONS_PER_HOUR", "10")))

    webhook_allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            h.strip() for h in _env("WEBHOOK_ALLOWED_HOSTS", "localhost,127.0.0.1,webhook-receiver").split(",") if h.strip()
        )
    )
    seed_webhook_url: str = field(default_factory=lambda: _env("SEED_WEBHOOK_URL", ""))
    seed_webhook_secret: str = field(default_factory=lambda: _env("SEED_WEBHOOK_SECRET", ""))
    dispatcher_enabled: bool = field(default_factory=lambda: _env("WEBHOOK_DISPATCHER", "on") == "on")

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"
