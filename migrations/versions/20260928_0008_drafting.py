"""Record the owner's drafting consent and how each generated draft version was made.

Revision ID: 20260928_0008
Revises: 20260928_0007
Create Date: 2026-09-28

Every change is a new table, so existing rows are untouched. ``owner_consents`` mirrors
``ai_consents`` without an account column: consent to AI drafting belongs to the owner
(ADR 0013). ``draft_generations`` keeps one record per generated version and cascades with
it, so pruning a version removes its record. Downgrading drops both tables; generated
versions stay, without their records.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0008"
down_revision: str | None = "20260928_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TONE_CHECK = "tone IN ('neutral','warm','formal','direct')"
_LENGTH_CHECK = "length IN ('short','medium','long')"


def upgrade() -> None:
    op.create_table(
        "owner_consents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("disclosure_version", sa.String(length=16), nullable=False),
        sa.Column("granted_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_owner_consents")),
        sa.UniqueConstraint(
            "provider", "scope", "disclosure_version", name="uq_owner_consents_scope"
        ),
    )

    op.create_table(
        "draft_generations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("tone", sa.String(length=16), nullable=False),
        sa.Column("length", sa.String(length=16), nullable=False),
        sa.Column("parts_json", sa.JSON(), nullable=False),
        sa.Column("instructions", sa.Text(), server_default="", nullable=False),
        sa.Column("missing_context_json", sa.JSON(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_TONE_CHECK, name=op.f("ck_draft_generations_tone_known")),
        sa.CheckConstraint(_LENGTH_CHECK, name=op.f("ck_draft_generations_length_known")),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["draft_versions.id"],
            name=op.f("fk_draft_generations_version_id_draft_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_draft_generations")),
        sa.UniqueConstraint("version_id", name="uq_draft_generations_version"),
    )


def downgrade() -> None:
    op.drop_table("draft_generations")
    op.drop_table("owner_consents")
