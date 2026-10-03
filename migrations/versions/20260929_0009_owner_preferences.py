"""Save the owner's preferences in one revisioned row.

Revision ID: 20260929_0009
Revises: 20260928_0008
Create Date: 2026-09-29

The change is a new table, so existing rows are untouched. ``owner_preferences`` holds at
most one row (``id = 1``), shared by the desktop and the CLI (ADR 0014); no row means the
defaults, and a NULL AI limit means the built-in default. The bounds below are those of
this revision and stay fixed here even if the application's limits change later.
Downgrading drops the table and the preferences in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0009"
down_revision: str | None = "20260928_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TONE_CHECK = "draft_tone IN ('neutral','warm','formal','direct')"
_LENGTH_CHECK = "draft_length IN ('short','medium','long')"
_RANGES = (
    ("ai_batch_size", 1, 10),
    ("ai_body_character_limit", 1, 8000),
    ("ai_max_output_tokens", 256, 64000),
    ("ai_max_requests_per_run", 1, 1000),
    ("ai_timeout_seconds", 10, 600),
)


def upgrade() -> None:
    op.create_table(
        "owner_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("time_zone", sa.String(length=64), nullable=True),
        sa.Column("shortlist_limit", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column("excluded_senders_json", sa.JSON(), nullable=False),
        sa.Column("draft_tone", sa.String(length=16), server_default="neutral", nullable=False),
        sa.Column("draft_length", sa.String(length=16), server_default="medium", nullable=False),
        sa.Column("ai_batch_size", sa.Integer(), nullable=True),
        sa.Column("ai_body_character_limit", sa.Integer(), nullable=True),
        sa.Column("ai_max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("ai_max_requests_per_run", sa.Integer(), nullable=True),
        sa.Column("ai_timeout_seconds", sa.Float(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_owner_preferences_single_row")),
        sa.CheckConstraint(
            "shortlist_limit BETWEEN 1 AND 10",
            name=op.f("ck_owner_preferences_shortlist_limit_range"),
        ),
        sa.CheckConstraint(_TONE_CHECK, name=op.f("ck_owner_preferences_draft_tone_known")),
        sa.CheckConstraint(_LENGTH_CHECK, name=op.f("ck_owner_preferences_draft_length_known")),
        *(
            sa.CheckConstraint(
                f"{column} IS NULL OR {column} BETWEEN {low} AND {high}",
                name=op.f(f"ck_owner_preferences_{column}_range"),
            )
            for column, low, high in _RANGES
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_owner_preferences_revision_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_owner_preferences")),
    )


def downgrade() -> None:
    op.drop_table("owner_preferences")
