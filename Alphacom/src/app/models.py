"""Tables for the data this service owns (Postgres, via SQLAlchemy).

Cases, entitlements and usage stay in their systems of record; we never copy them
here. Rationale for each table and index is in DECISIONS.md ("Data model").
"""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    # Keys are scoped per caller ("<sub>|<client_id>"), so two clients can't collide.
    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(String(64))  # catches key reuse with a different body
    state: Mapped[str] = mapped_column(String(16))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    response_headers: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    expires_at: Mapped[datetime]

    __table_args__ = (
        CheckConstraint("state IN ('in_progress', 'completed')", name="ck_idempotency_state"),
        Index("ix_idempotency_expires", "expires_at"),  # the sweeper deletes by expiry
    )


class AuditLog(Base):
    """Append-only. Enforced twice: a trigger rejects UPDATE/DELETE, and the app's DB
    role only has SELECT + INSERT on this table (see the migration)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    correlation_id: Mapped[str] = mapped_column(String(128))
    subject: Mapped[str | None] = mapped_column(Text)    # the human
    actor: Mapped[str | None] = mapped_column(Text)      # the agent acting for them, if any
    client_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)            # "POST /v1/cases/{caseId}/escalate"
    resource: Mapped[str | None] = mapped_column(Text)   # "case:CS-10231"
    outcome: Mapped[str] = mapped_column(String(16))
    http_status: Mapped[int] = mapped_column(Integer)
    detail: Mapped[dict | None] = mapped_column(JSONB)   # small; never bodies or tokens

    __table_args__ = (
        CheckConstraint("outcome IN ('success', 'denied', 'error')", name="ck_audit_outcome"),
        # The three questions audit gets asked: what happened to this resource, what did
        # this user/agent do, and every line for this request.
        Index("ix_audit_resource", "resource", text("occurred_at DESC")),
        Index("ix_audit_subject", "subject", text("occurred_at DESC")),
        Index("ix_audit_actor", "actor", text("occurred_at DESC"), postgresql_where=text("actor IS NOT NULL")),
        Index("ix_audit_correlation", "correlation_id"),
    )


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    secret: Mapped[str] = mapped_column(Text)  # needed to sign, so not hashable; KMS-encrypted in prod
    events: Mapped[list[str]] = mapped_column(ARRAY(Text))
    active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class WebhookDelivery(Base):
    """Transactional outbox: one row per (event, subscription), polled by the dispatcher."""

    __tablename__ = "webhook_outbox"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(40))  # sent as Webhook-Id; receivers dedupe on it
    subscription_id: Mapped[str] = mapped_column(ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)  # exact bytes that get signed and sent
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    delivered_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'delivered', 'dead')", name="ck_outbox_status"),
        # Dispatcher: pending rows that are due. Partial, so delivered history doesn't bloat it.
        Index("ix_outbox_due", "next_attempt_at", postgresql_where=text("status = 'pending'")),
        Index("ix_outbox_event", "event_id"),
    )
