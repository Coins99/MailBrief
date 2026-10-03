"""Store follow-up signals on analyses and the proposals they make for actions.

Revision ID: 20260930_0011
Revises: 20260930_0010
Create Date: 2026-09-30

``analyses`` gains the follow-up signal (ADR 0016): its kind, 'none' for every existing row,
and its quote. Rebuilding ``analyses`` keeps its rows, CHECK constraints, cascade and index,
and Alembic's connection leaves SQLite foreign keys off, so the rebuild does not fire
ON DELETE SET NULL on ``digest_items.analysis_id``. The new ``action_proposals`` table holds
each proposed update with a snapshot of its email. Downgrading drops both and keeps every
other row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0011"
down_revision: str | None = "20260930_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "action_proposals"
_INDEX = "ix_action_proposals_action_id"
_FOLLOW_UP_CHECK = "follow_up_kind IN ('none','new_deadline','cancelled','delivered')"
_KIND_CHECK = "kind IN ('new_deadline','cancelled','delivered')"
_STATE_CHECK = "state IN ('pending','applied','dismissed')"
_PRECISION_CHECK = "deadline_precision IN ('none','unresolved','date','datetime')"


def upgrade() -> None:
    with op.batch_alter_table("analyses", recreate="always") as batch:
        batch.add_column(
            sa.Column("follow_up_kind", sa.String(length=16), nullable=False, server_default="none")
        )
        batch.add_column(sa.Column("follow_up_evidence", sa.String(length=160), nullable=True))
        batch.create_check_constraint(op.f("ck_analyses_follow_up_kind_known"), _FOLLOW_UP_CHECK)

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("deadline_text", sa.Text(), nullable=True),
        sa.Column(
            "deadline_precision", sa.String(length=16), server_default="none", nullable=False
        ),
        sa.Column("deadline_date", sa.Date(), nullable=True),
        sa.Column("deadline_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_timezone", sa.String(length=128), nullable=True),
        sa.Column("suggested_target_date", sa.Date(), nullable=True),
        sa.Column("target_reason", sa.String(length=32), nullable=True),
        sa.Column("evidence", sa.String(length=160), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=255), nullable=False),
        sa.Column("provider_message_id", sa.String(length=512), nullable=False),
        sa.Column("provider_thread_id", sa.String(length=512), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("sender_address", sa.String(length=320), nullable=False),
        sa.Column("web_link", sa.Text(), nullable=False),
        sa.Column("received_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_json", sa.JSON(), nullable=True),
        sa.Column("source_added", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("applied_revision", sa.Integer(), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_KIND_CHECK, name=op.f("ck_action_proposals_kind_known")),
        sa.CheckConstraint(_STATE_CHECK, name=op.f("ck_action_proposals_state_known")),
        sa.CheckConstraint(
            _PRECISION_CHECK, name=op.f("ck_action_proposals_deadline_precision_known")
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_action_proposals_action_id_actions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_action_proposals_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_proposals")),
        sa.UniqueConstraint(
            "action_id", "provider_message_id", "kind", name="uq_action_proposals_identity"
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(_INDEX, _TABLE, ["action_id"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
    with op.batch_alter_table("analyses", recreate="always") as batch:
        batch.drop_constraint(op.f("ck_analyses_follow_up_kind_known"), type_="check")
        batch.drop_column("follow_up_evidence")
        batch.drop_column("follow_up_kind")
