"""Key suggestion decisions to the Gmail message and never reuse suggestion IDs.

Revision ID: 20260928_0006
Revises: 20260927_0005
Create Date: 2026-09-28

``suggestion_decisions`` was keyed by the internal ``messages.id`` and cascaded with it,
so deleting and re-syncing a message lost the owner's decisions. The rebuilt table keys
each decision by provider, provider account ID, provider message ID and title
fingerprint, with no foreign key to messages or accounts. Every existing decision has its
message, because the old foreign key cascaded, so each row is copied with the same ID,
decision, action and time, and its identity filled in from its message and account.

``action_suggestions`` is rebuilt with AUTOINCREMENT, keeping its rows, IDs, constraints
and cascade, so a deleted suggestion's ID is never given to another one. Copying the rows
sets ``sqlite_sequence`` to the highest current ID. IDs freed above it before this
revision can still be given out once, because SQLite kept no record of them.

Alembic's connection leaves SQLite foreign keys off, so neither rebuild fires ON DELETE
actions. Downgrading restores ``message_id`` by joining each decision back to its
message, and drops every decision whose message is no longer stored, because the old
schema cannot hold it.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0006"
down_revision: str | None = "20260927_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DECISION_CHECK = "decision IN ('accepted','dismissed')"
_INDEX = "ix_suggestion_decisions_action_id"
_TABLE = "suggestion_decisions"
_NEW = "suggestion_decisions_new"


def _decision_columns() -> list[sa.Column[Any]]:
    """The columns both layouts have after their message key."""
    return [
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("action_id", sa.Integer(), nullable=True),
        sa.Column("decided_at_utc", sa.DateTime(timezone=True), nullable=False),
    ]


def _decision_constraints() -> list[sa.Constraint]:
    """The constraints both layouts share, with the names revision 0005 gave them."""
    return [
        sa.CheckConstraint(_DECISION_CHECK, name=op.f("ck_suggestion_decisions_decision_known")),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_suggestion_decisions_action_id_actions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suggestion_decisions")),
    ]


def _replace_decisions(copy_rows: str) -> None:
    """Swap in the new table after filling it with ``copy_rows``, and recreate the index."""
    op.execute(copy_rows)
    op.drop_table(_TABLE)
    op.rename_table(_NEW, _TABLE)
    op.create_index(_INDEX, _TABLE, ["action_id"])


def upgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.create_table(
        _NEW,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_account_id", sa.String(length=255), nullable=False),
        sa.Column("provider_message_id", sa.String(length=512), nullable=False),
        *_decision_columns(),
        *_decision_constraints(),
        sa.UniqueConstraint(
            "provider",
            "provider_account_id",
            "provider_message_id",
            "fingerprint",
            name=op.f("uq_suggestion_decisions_identity"),
        ),
    )
    _replace_decisions(
        f"INSERT INTO {_NEW} (id, provider, provider_account_id, provider_message_id, "
        "fingerprint, decision, action_id, decided_at_utc) "
        "SELECT d.id, a.provider, a.provider_account_id, m.provider_message_id, "
        "d.fingerprint, d.decision, d.action_id, d.decided_at_utc "
        f"FROM {_TABLE} AS d "
        "JOIN messages AS m ON m.id = d.message_id "
        "JOIN accounts AS a ON a.id = m.account_id"
    )

    with op.batch_alter_table(
        "action_suggestions",
        recreate="always",
        table_kwargs={"sqlite_autoincrement": True},
    ):
        pass


def downgrade() -> None:
    with op.batch_alter_table("action_suggestions", recreate="always") as batch:
        # AUTOINCREMENT kept the key inline and unnamed; revision 0005 named it.
        batch.create_primary_key(op.f("pk_action_suggestions"), ["id"])

    op.drop_index(_INDEX, table_name=_TABLE)
    op.create_table(
        _NEW,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        *_decision_columns(),
        *_decision_constraints(),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_suggestion_decisions_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "message_id", "fingerprint", name=op.f("uq_suggestion_decisions_fingerprint")
        ),
    )
    # The inner joins leave out every decision whose message is no longer stored.
    _replace_decisions(
        f"INSERT INTO {_NEW} (id, message_id, fingerprint, decision, action_id, "
        "decided_at_utc) "
        "SELECT d.id, m.id, d.fingerprint, d.decision, d.action_id, d.decided_at_utc "
        f"FROM {_TABLE} AS d "
        "JOIN accounts AS a ON a.provider = d.provider "
        "AND a.provider_account_id = d.provider_account_id "
        "JOIN messages AS m ON m.account_id = a.id "
        "AND m.provider_message_id = d.provider_message_id"
    )
