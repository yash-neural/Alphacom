from app.db import Database
from app.models import AuditLog


async def record(db: Database, *, correlation_id: str, action: str, outcome: str, http_status: int,
                 principal=None, resource: str | None = None, detail: dict | None = None) -> None:
    """Append one audit entry. Never pass request bodies or tokens in detail."""
    async with db.session() as s, s.begin():
        s.add(AuditLog(
            correlation_id=correlation_id,
            subject=getattr(principal, "sub", None),
            actor=getattr(principal, "actor", None),
            client_id=getattr(principal, "client_id", None),
            action=action,
            resource=resource,
            outcome=outcome,
            http_status=http_status,
            detail=detail,
        ))
