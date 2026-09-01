"""Create the initial MailBrief schema.

Revision ID: 20260831_0001
Revises:
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all MVP persistence tables, constraints, and indexes."""
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=255), nullable=False),
        sa.Column("email_address", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("tenant_id", sa.String(length=255), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sync_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.UniqueConstraint(
            "provider",
            "provider_account_id",
            name="uq_accounts_provider_identity",
        ),
    )
    op.create_index("ix_accounts_email_address", "accounts", ["email_address"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("provider_message_id", sa.String(length=512), nullable=False),
        sa.Column("internet_message_id", sa.String(length=998), nullable=True),
        sa.Column("conversation_id", sa.String(length=512), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("sender_name", sa.String(length=255), nullable=True),
        sa.Column("sender_address", sa.String(length=320), nullable=False),
        sa.Column("to_recipients_json", sa.JSON(), nullable=False),
        sa.Column("received_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_read", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("importance", sa.String(length=16), nullable=False),
        sa.Column("has_attachments", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("body_preview", sa.Text(), nullable=False),
        sa.Column("web_link", sa.Text(), nullable=False),
        sa.Column("rank_score", sa.Integer(), nullable=True),
        sa.Column("rank_reasons_json", sa.JSON(), nullable=False),
        sa.Column("synced_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_messages_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "account_id",
            "provider_message_id",
            name="uq_messages_account_provider_message",
        ),
    )
    op.create_index(
        "ix_messages_account_received",
        "messages",
        ["account_id", "received_at_utc"],
    )
    op.create_index("ix_messages_account_rank", "messages", ["account_id", "rank_score"])

    op.create_table(
        "analyses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("action_required", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("action_text", sa.Text(), nullable=True),
        sa.Column("deadline_text", sa.Text(), nullable=True),
        sa.Column("deadline_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("analyzed_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_analyses_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_analyses_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analyses")),
        sa.UniqueConstraint(
            "message_id",
            "input_hash",
            "model",
            "prompt_version",
            name="uq_analyses_cache_key",
        ),
    )
    op.create_index("ix_analyses_message_id", "analyses", ["message_id"])

    op.create_table(
        "digests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("timezone_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("generated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_digests_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_digests")),
        sa.UniqueConstraint("account_id", "local_date", name="uq_digests_account_local_date"),
    )

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("range_start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("page_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("sanitized_error_code", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "message_count >= 0",
            name=op.f("ck_sync_runs_message_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "page_count >= 0",
            name=op.f("ck_sync_runs_page_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "range_end_utc > range_start_utc",
            name=op.f("ck_sync_runs_valid_range"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_sync_runs_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    op.create_index(
        "ix_sync_runs_account_started",
        "sync_runs",
        ["account_id", "started_at_utc"],
    )

    op.create_table(
        "digest_items",
        sa.Column("digest_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(length=32), nullable=False),
        sa.CheckConstraint("position >= 0", name=op.f("ck_digest_items_position_nonnegative")),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name=op.f("fk_digest_items_analysis_id_analyses"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["digest_id"],
            ["digests.id"],
            name=op.f("fk_digest_items_digest_id_digests"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_digest_items_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("digest_id", "message_id", name=op.f("pk_digest_items")),
        sa.UniqueConstraint("digest_id", "position", name="uq_digest_items_digest_position"),
    )


def downgrade() -> None:
    """Remove the complete initial schema in reverse dependency order."""
    op.drop_table("digest_items")
    op.drop_index("ix_sync_runs_account_started", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_table("digests")
    op.drop_index("ix_analyses_message_id", table_name="analyses")
    op.drop_table("analyses")
    op.drop_index("ix_messages_account_rank", table_name="messages")
    op.drop_index("ix_messages_account_received", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_accounts_email_address", table_name="accounts")
    op.drop_table("accounts")
