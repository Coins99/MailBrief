"""Store accepted actions, their steps and sources, suggestions and the owner's decisions.

Revision ID: 20260927_0005
Revises: 20260925_0004
Create Date: 2026-09-27

Every change is a new table, so existing rows are untouched. Actions have no account
foreign key: they belong to the owner and keep a snapshot of each source message, so
deleting an account or message only sets ``action_sources.message_id`` to NULL.
Downgrading drops every action, suggestion and decision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0005"
down_revision: str | None = "20260925_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRECISION_CHECK = "deadline_precision IN ('none','unresolved','date','datetime')"
_OWNERSHIP_CHECK = "ownership IN ('mine','waiting_for')"


def upgrade() -> None:
    op.create_table(
        "actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("ownership", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("effort", sa.String(length=16), nullable=True),
        sa.Column("deadline_text", sa.Text(), nullable=True),
        sa.Column(
            "deadline_precision", sa.String(length=16), server_default="none", nullable=False
        ),
        sa.Column("deadline_date", sa.Date(), nullable=True),
        sa.Column("deadline_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_timezone", sa.String(length=128), nullable=True),
        sa.Column("suggested_target_date", sa.Date(), nullable=True),
        sa.Column("target_reason", sa.String(length=32), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint("status IN ('open','completed')", name=op.f("ck_actions_status_known")),
        sa.CheckConstraint(_OWNERSHIP_CHECK, name=op.f("ck_actions_ownership_known")),
        sa.CheckConstraint(_PRECISION_CHECK, name=op.f("ck_actions_deadline_precision_known")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_actions_revision_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_actions")),
        sa.UniqueConstraint("public_id", name="uq_actions_public_id"),
    )

    op.create_table(
        "action_suggestions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("ownership", sa.String(length=16), nullable=False),
        sa.Column("effort", sa.String(length=16), nullable=True),
        sa.Column("deadline_text", sa.Text(), nullable=True),
        sa.Column(
            "deadline_precision", sa.String(length=16), server_default="none", nullable=False
        ),
        sa.Column("deadline_date", sa.Date(), nullable=True),
        sa.Column("deadline_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_timezone", sa.String(length=128), nullable=True),
        sa.Column("suggested_target_date", sa.Date(), nullable=True),
        sa.Column("target_reason", sa.String(length=32), nullable=True),
        sa.Column("steps_json", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "position >= 0", name=op.f("ck_action_suggestions_position_nonnegative")
        ),
        sa.CheckConstraint(_OWNERSHIP_CHECK, name=op.f("ck_action_suggestions_ownership_known")),
        sa.CheckConstraint(
            _PRECISION_CHECK, name=op.f("ck_action_suggestions_deadline_precision_known")
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name=op.f("fk_action_suggestions_analysis_id_analyses"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_suggestions")),
        sa.UniqueConstraint("analysis_id", "position", name="uq_action_suggestions_position"),
        sa.UniqueConstraint("analysis_id", "fingerprint", name="uq_action_suggestions_fingerprint"),
    )

    op.create_table(
        "action_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("done", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("done_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("position >= 0", name=op.f("ck_action_steps_position_nonnegative")),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_action_steps_action_id_actions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_steps")),
    )

    op.create_table(
        "action_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("sender_address", sa.String(length=320), nullable=False),
        sa.Column("web_link", sa.Text(), nullable=False),
        sa.Column("received_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_action_sources_action_id_actions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_action_sources_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_sources")),
        sa.UniqueConstraint("action_id", "provider_message_id", name="uq_action_sources_message"),
    )

    op.create_table(
        "suggestion_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("action_id", sa.Integer(), nullable=True),
        sa.Column("decided_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('accepted','dismissed')",
            name=op.f("ck_suggestion_decisions_decision_known"),
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_suggestion_decisions_action_id_actions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_suggestion_decisions_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suggestion_decisions")),
        sa.UniqueConstraint(
            "message_id", "fingerprint", name="uq_suggestion_decisions_fingerprint"
        ),
    )

    op.create_index("ix_actions_status", "actions", ["status"])
    op.create_index("ix_action_steps_action_id", "action_steps", ["action_id"])
    op.create_index("ix_action_sources_message_id", "action_sources", ["message_id"])
    op.create_index("ix_suggestion_decisions_action_id", "suggestion_decisions", ["action_id"])


def downgrade() -> None:
    op.drop_index("ix_suggestion_decisions_action_id", table_name="suggestion_decisions")
    op.drop_index("ix_action_sources_message_id", table_name="action_sources")
    op.drop_index("ix_action_steps_action_id", table_name="action_steps")
    op.drop_index("ix_actions_status", table_name="actions")
    op.drop_table("suggestion_decisions")
    op.drop_table("action_sources")
    op.drop_table("action_steps")
    op.drop_table("action_suggestions")
    op.drop_table("actions")
