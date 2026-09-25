"""Track cached Inbox membership and failed metadata counts without deleting mail."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260925_0002"
down_revision: str | None = "20260831_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("is_in_inbox", sa.Boolean(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "sync_runs",
        sa.Column(
            "failed_message_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("sync_runs") as batch:
        batch.drop_column("failed_message_count")
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("is_in_inbox")
