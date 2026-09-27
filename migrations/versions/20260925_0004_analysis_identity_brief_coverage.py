"""Version the analysis cache identity, record brief coverage and store AI consent.

Revision ID: 20260925_0004
Revises: 20260925_0003
Create Date: 2026-09-25

Rebuilding ``analyses`` keeps its rows, CHECK constraints, cascade and index. Alembic's
connection leaves SQLite foreign keys off, so the rebuild does not fire ON DELETE SET NULL
on ``digest_items.analysis_id``. Downgrading fails if two analyses now differ only by
provider or schema version, because the restored cache key cannot hold both.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260925_0004"
down_revision: str | None = "20260925_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CACHE_KEY = ["message_id", "input_hash", "model", "prompt_version"]
_CACHE_IDENTITY = [
    "message_id",
    "input_hash",
    "provider",
    "model",
    "prompt_version",
    "schema_version",
]
_PRECISION_CHECK = "deadline_precision IN ('none','unresolved','date','datetime')"
_COUNT_COLUMNS = (
    "shortlisted_count",
    "analyzed_count",
    "reused_count",
    "failed_count",
    "skipped_count",
    "input_tokens",
    "output_tokens",
)


def upgrade() -> None:
    with op.batch_alter_table("analyses", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "deadline_precision",
                sa.String(length=16),
                nullable=False,
                server_default="none",
            )
        )
        batch.add_column(sa.Column("deadline_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("deadline_timezone", sa.String(length=128), nullable=True))
        batch.drop_constraint("uq_analyses_cache_key", type_="unique")
        batch.create_unique_constraint("uq_analyses_cache_identity", _CACHE_IDENTITY)
        batch.create_check_constraint(
            op.f("ck_analyses_deadline_precision_known"),
            _PRECISION_CHECK,
        )

    op.add_column("digests", sa.Column("sync_complete", sa.Boolean(), nullable=True))
    for name in _COUNT_COLUMNS:
        op.add_column("digests", sa.Column(name, sa.Integer(), nullable=True))
    op.add_column("digests", sa.Column("ai_provider", sa.String(length=64), nullable=True))
    op.add_column("digests", sa.Column("ai_model", sa.String(length=128), nullable=True))

    op.create_table(
        "ai_consents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("disclosure_version", sa.String(length=32), nullable=False),
        sa.Column("granted_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_ai_consents_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_consents")),
        sa.UniqueConstraint(
            "account_id",
            "provider",
            "disclosure_version",
            name="uq_ai_consents_scope",
        ),
    )


def downgrade() -> None:
    op.drop_table("ai_consents")

    with op.batch_alter_table("digests") as batch:
        batch.drop_column("ai_model")
        batch.drop_column("ai_provider")
        for name in reversed(_COUNT_COLUMNS):
            batch.drop_column(name)
        batch.drop_column("sync_complete")

    with op.batch_alter_table("analyses", recreate="always") as batch:
        batch.drop_constraint(op.f("ck_analyses_deadline_precision_known"), type_="check")
        batch.drop_constraint("uq_analyses_cache_identity", type_="unique")
        batch.create_unique_constraint("uq_analyses_cache_key", _OLD_CACHE_KEY)
        batch.drop_column("deadline_timezone")
        batch.drop_column("deadline_date")
        batch.drop_column("deadline_precision")
