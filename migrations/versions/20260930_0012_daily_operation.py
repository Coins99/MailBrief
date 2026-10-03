"""Automatic runs: refresh settings, the automatic-analysis permission, deferrals, declines.

Revision ID: 20260930_0012
Revises: 20260930_0011
Create Date: 2026-09-30

Every change adds a column, so existing rows are kept (ADR 0017). ``owner_preferences``
gains whether to refresh when MailBrief starts (off) and how often while it runs (NULL,
never, or 60, 120 or 240 minutes). ``ai_consents`` gains the automatic-analysis permission
on the consent it belongs to: how many messages an automatic run may send without asking (0,
none, to 10) and when it was given; existing consents carry none. ``digests`` gains a count
of messages an automatic run deferred (0 for every brief so far). ``messages`` gains when
the owner declined a message in a review. The two tables with new CHECK constraints are
rebuilt, which keeps their rows, constraints and cascade; Alembic's connection leaves SQLite
foreign keys off, so the rebuild of ``ai_consents`` does not cascade. The bounds below are
those of this revision and stay fixed here even if the application's limits change later.
Downgrading drops all of these and keeps every row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0012"
down_revision: str | None = "20260930_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INTERVAL_CHECK = "refresh_interval_minutes IS NULL OR refresh_interval_minutes IN (60, 120, 240)"
_INTERVAL_NAME = "ck_owner_preferences_refresh_interval_known"
_LIMIT_CHECK = "auto_send_limit BETWEEN 0 AND 10"
_LIMIT_NAME = "ck_ai_consents_auto_send_limit_range"


def upgrade() -> None:
    with op.batch_alter_table("owner_preferences", recreate="always") as batch:
        batch.add_column(
            sa.Column("refresh_on_launch", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch.add_column(sa.Column("refresh_interval_minutes", sa.Integer(), nullable=True))
        batch.create_check_constraint(op.f(_INTERVAL_NAME), _INTERVAL_CHECK)
    with op.batch_alter_table("ai_consents", recreate="always") as batch:
        batch.add_column(
            sa.Column("auto_send_limit", sa.Integer(), server_default=sa.text("0"), nullable=False)
        )
        batch.add_column(
            sa.Column("auto_send_granted_at_utc", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_check_constraint(op.f(_LIMIT_NAME), _LIMIT_CHECK)
    op.add_column(
        "digests",
        sa.Column("deferred_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "messages", sa.Column("review_declined_at_utc", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("review_declined_at_utc")
    with op.batch_alter_table("digests") as batch:
        batch.drop_column("deferred_count")
    with op.batch_alter_table("ai_consents", recreate="always") as batch:
        batch.drop_constraint(op.f(_LIMIT_NAME), type_="check")
        batch.drop_column("auto_send_granted_at_utc")
        batch.drop_column("auto_send_limit")
    with op.batch_alter_table("owner_preferences", recreate="always") as batch:
        batch.drop_constraint(op.f(_INTERVAL_NAME), type_="check")
        batch.drop_column("refresh_interval_minutes")
        batch.drop_column("refresh_on_launch")
