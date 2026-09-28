"""Store the owner's drafts and notes, their saved versions and their source snapshots.

Revision ID: 20260928_0007
Revises: 20260928_0006
Create Date: 2026-09-28

Every change is a new table, so existing rows are untouched. Drafts have no account
foreign key: they belong to the owner (ADR 0012). Deleting an action or a message only
sets ``drafts.action_id`` or ``draft_sources.message_id`` to NULL; the action's title and
each source's subject, sender, link and received time stay as snapshots. Downgrading drops
every draft.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0007"
down_revision: str | None = "20260928_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_CHECK = "kind IN ('reply','email','note','message')"
_ORIGIN_CHECK = "origin IN ('created','edited','restored','generated')"


def _text_columns() -> list[sa.Column[str]]:
    """The owner's text, which drafts and their versions both hold."""
    return [
        sa.Column(name, sa.Text(), server_default="", nullable=False)
        for name in ("title", "to_text", "cc_text", "body")
    ]


def upgrade() -> None:
    op.create_table(
        "drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        *_text_columns(),
        sa.Column("action_id", sa.Integer(), nullable=True),
        sa.Column("action_title", sa.Text(), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(_KIND_CHECK, name=op.f("ck_drafts_kind_known")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_drafts_revision_positive")),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.id"],
            name=op.f("fk_drafts_action_id_actions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drafts")),
        sa.UniqueConstraint("public_id", name="uq_drafts_public_id"),
    )

    op.create_table(
        "draft_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        *_text_columns(),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("number >= 1", name=op.f("ck_draft_versions_number_positive")),
        sa.CheckConstraint(_ORIGIN_CHECK, name=op.f("ck_draft_versions_origin_known")),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["drafts.id"],
            name=op.f("fk_draft_versions_draft_id_drafts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_draft_versions")),
        sa.UniqueConstraint("draft_id", "number", name="uq_draft_versions_number"),
    )

    op.create_table(
        "draft_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("sender_address", sa.String(length=320), nullable=False),
        sa.Column("web_link", sa.Text(), nullable=False),
        sa.Column("received_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["drafts.id"],
            name=op.f("fk_draft_sources_draft_id_drafts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_draft_sources_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_draft_sources")),
        sa.UniqueConstraint("draft_id", "provider_message_id", name="uq_draft_sources_message"),
    )

    op.create_index("ix_drafts_updated_at_utc", "drafts", ["updated_at_utc"])
    op.create_index("ix_drafts_action_id", "drafts", ["action_id"])
    op.create_index("ix_draft_sources_message_id", "draft_sources", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_draft_sources_message_id", table_name="draft_sources")
    op.drop_index("ix_drafts_action_id", table_name="drafts")
    op.drop_index("ix_drafts_updated_at_utc", table_name="drafts")
    op.drop_table("draft_sources")
    op.drop_table("draft_versions")
    op.drop_table("drafts")
