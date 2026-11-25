"""Initial schema: idempotency keys, audit log, webhook subscriptions and outbox.

Revision ID: 0001
Revises:
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("response_status", sa.Integer()),
        sa.Column("response_body", postgresql.JSONB()),
        sa.Column("response_headers", postgresql.JSONB()),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.CheckConstraint("state IN ('in_progress', 'completed')", name="ck_idempotency_state"),
    )
    op.create_index("ix_idempotency_expires", "idempotency_keys", ["expires_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("occurred_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("subject", sa.Text()),
        sa.Column("actor", sa.Text()),
        sa.Column("client_id", sa.Text()),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text()),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("detail", postgresql.JSONB()),
        sa.CheckConstraint("outcome IN ('success', 'denied', 'error')", name="ck_audit_outcome"),
    )
    op.create_index("ix_audit_resource", "audit_log", ["resource", sa.text("occurred_at DESC")])
    op.create_index("ix_audit_subject", "audit_log", ["subject", sa.text("occurred_at DESC")])
    op.create_index("ix_audit_actor", "audit_log", ["actor", sa.text("occurred_at DESC")],
                    postgresql_where=sa.text("actor IS NOT NULL"))
    op.create_index("ix_audit_correlation", "audit_log", ["correlation_id"])

    # Append-only, enforced by the database rather than by convention.
    op.execute("""
        CREATE FUNCTION audit_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
    """)

    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("events", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "webhook_outbox",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("event_id", sa.String(40), nullable=False),
        sa.Column("subscription_id", sa.String(40),
                  sa.ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("delivered_at", TS),
        sa.CheckConstraint("status IN ('pending', 'delivered', 'dead')", name="ck_outbox_status"),
    )
    op.create_index("ix_outbox_due", "webhook_outbox", ["next_attempt_at"],
                    postgresql_where=sa.text("status = 'pending'"))
    op.create_index("ix_outbox_event", "webhook_outbox", ["event_id"])

    # Least privilege for the app's role (created by docker/postgres-init.sql). Note
    # audit_log gets SELECT + INSERT only.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON idempotency_keys, webhook_subscriptions, webhook_outbox TO api;
                GRANT SELECT, INSERT ON audit_log TO api;
                GRANT USAGE ON SEQUENCE audit_log_id_seq TO api;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.drop_table("webhook_outbox")
    op.drop_table("webhook_subscriptions")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")
    op.drop_table("audit_log")
    op.drop_table("idempotency_keys")
