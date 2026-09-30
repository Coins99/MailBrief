"""Track the threads of open actions: sent messages, source snapshots and a seen watermark.

Revision ID: 20260930_0010
Revises: 20260929_0009
Create Date: 2026-09-30

Every change adds a column or an index, so existing rows are kept (ADR 0015).
``messages.is_sent`` marks messages sent from the account, which are cached only inside
tracked threads; a new index finds an account's messages by thread. ``action_sources``
gains its message's provider, account and thread, backfilled from the linked message and
account; a source whose email had already left local mail stays NULL and is not tracked.
``actions.thread_seen_until_utc`` is the owner's "seen" watermark. Downgrading drops all
of these and keeps every row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0010"
down_revision: str | None = "20260929_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_messages_account_conversation"
_BACKFILL = """
UPDATE action_sources
SET provider = (
        SELECT accounts.provider FROM messages
        JOIN accounts ON accounts.id = messages.account_id
        WHERE messages.id = action_sources.message_id
    ),
    provider_account_id = (
        SELECT accounts.provider_account_id FROM messages
        JOIN accounts ON accounts.id = messages.account_id
        WHERE messages.id = action_sources.message_id
    ),
    provider_thread_id = (
        SELECT messages.conversation_id FROM messages
        WHERE messages.id = action_sources.message_id
    )
WHERE message_id IS NOT NULL
"""


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("is_sent", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_index(_INDEX, "messages", ["account_id", "conversation_id"])
    op.add_column("action_sources", sa.Column("provider", sa.String(length=32), nullable=True))
    op.add_column(
        "action_sources", sa.Column("provider_account_id", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "action_sources", sa.Column("provider_thread_id", sa.String(length=512), nullable=True)
    )
    op.execute(_BACKFILL)
    op.add_column(
        "actions", sa.Column("thread_seen_until_utc", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("actions") as batch:
        batch.drop_column("thread_seen_until_utc")
    with op.batch_alter_table("action_sources") as batch:
        batch.drop_column("provider_thread_id")
        batch.drop_column("provider_account_id")
        batch.drop_column("provider")
    op.drop_index(_INDEX, table_name="messages")
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("is_sent")
