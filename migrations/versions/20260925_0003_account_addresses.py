"""Add accounts.account_addresses to databases created before the column existed.

Revision ID: 20260925_0003
Revises: 20260925_0002
Create Date: 2026-09-25

Databases created from an earlier development copy of the initial revision may
already contain the column, so the upgrade only adds it when it is missing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260925_0003"
down_revision: str | None = "20260925_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("accounts", "account_addresses"):
        op.add_column("accounts", sa.Column("account_addresses", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("account_addresses")
