"""SQLAlchemy table mappings for local MailBrief state."""

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from mailbrief.storage.types import UTCDateTime


def utc_now() -> datetime:
    """Return the current aware UTC timestamp for ORM defaults."""
    return datetime.now(UTC)


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_PRECISION_CHECK = "deadline_precision IN ('none','unresolved','date','datetime')"
_OWNERSHIP_CHECK = "ownership IN ('mine','waiting_for')"


class Base(DeclarativeBase):
    """Declarative base shared by mappings and Alembic metadata."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class AccountTable(Base):
    """One connected provider account."""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_account_id",
            name="uq_accounts_provider_identity",
        ),
        Index("ix_accounts_email_address", "email_address"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email_address: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    tenant_id: Mapped[str | None] = mapped_column(String(255))
    account_addresses: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    last_sync_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MessageTable(Base):
    """Normalized provider message metadata; full bodies are never stored."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "provider_message_id",
            name="uq_messages_account_provider_message",
        ),
        Index("ix_messages_account_received", "account_id", "received_at_utc"),
        Index("ix_messages_account_rank", "account_id", "rank_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    internet_message_id: Mapped[str | None] = mapped_column(String(998))
    conversation_id: Mapped[str | None] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sender_name: Mapped[str | None] = mapped_column(String(255))
    sender_address: Mapped[str] = mapped_column(String(320), nullable=False)
    to_recipients_json: Mapped[list[dict[str, str | None]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    received_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    is_read: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    importance: Mapped[str] = mapped_column(String(16), nullable=False)
    is_in_inbox: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    has_attachments: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    body_preview: Mapped[str] = mapped_column(Text, nullable=False, default="")
    web_link: Mapped[str] = mapped_column(Text, nullable=False)
    rank_score: Mapped[int | None] = mapped_column(Integer)
    rank_reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    synced_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class AnalysisTable(Base):
    """Cached structured AI result for one versioned message input."""

    __tablename__ = "analyses"
    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "input_hash",
            "provider",
            "model",
            "prompt_version",
            "schema_version",
            name="uq_analyses_cache_identity",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_range",
        ),
        CheckConstraint(
            "deadline_precision IN ('none','unresolved','date','datetime')",
            name="deadline_precision_known",
        ),
        Index("ix_analyses_message_id", "message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    action_required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    action_text: Mapped[str | None] = mapped_column(Text)
    deadline_text: Mapped[str | None] = mapped_column(Text)
    deadline_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deadline_precision: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="none",
        server_default="none",
    )
    deadline_date: Mapped[date | None] = mapped_column(Date)
    deadline_timezone: Mapped[str | None] = mapped_column(String(128))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    analyzed_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class DigestTable(Base):
    """One daily brief for one account and local date."""

    __tablename__ = "digests"
    __table_args__ = (
        UniqueConstraint("account_id", "local_date", name="uq_digests_account_local_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    # Brief coverage; every column is NULL for briefs saved before M4.
    sync_complete: Mapped[bool | None] = mapped_column(Boolean)
    shortlisted_count: Mapped[int | None] = mapped_column(Integer)
    analyzed_count: Mapped[int | None] = mapped_column(Integer)
    reused_count: Mapped[int | None] = mapped_column(Integer)
    failed_count: Mapped[int | None] = mapped_column(Integer)
    skipped_count: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    ai_provider: Mapped[str | None] = mapped_column(String(64))
    ai_model: Mapped[str | None] = mapped_column(String(128))


class AIConsentTable(Base):
    """Per-account consent to send minimized email content to one AI provider."""

    __tablename__ = "ai_consents"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "provider",
            "disclosure_version",
            name="uq_ai_consents_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    disclosure_version: Mapped[str] = mapped_column(String(32), nullable=False)
    granted_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())


class DigestItemTable(Base):
    """Ordered membership of a message and analysis in a digest."""

    __tablename__ = "digest_items"
    __table_args__ = (
        UniqueConstraint("digest_id", "position", name="uq_digest_items_digest_position"),
        CheckConstraint("position >= 0", name="position_nonnegative"),
    )

    digest_id: Mapped[int] = mapped_column(
        ForeignKey("digests.id", ondelete="CASCADE"),
        primary_key=True,
    )
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("analyses.id", ondelete="SET NULL"))
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    section: Mapped[str] = mapped_column(String(32), nullable=False)


class SyncRunTable(Base):
    """Auditable state of one mailbox synchronization attempt."""

    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint("range_end_utc > range_start_utc", name="valid_range"),
        CheckConstraint("page_count >= 0", name="page_count_nonnegative"),
        CheckConstraint("message_count >= 0", name="message_count_nonnegative"),
        Index("ix_sync_runs_account_started", "account_id", "started_at_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    range_start_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    range_end_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    completed_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())
    page_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    message_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    sanitized_error_code: Mapped[str | None] = mapped_column(String(128))
    failed_message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class ActionTable(Base):
    """An action the owner accepted.

    Actions have no account foreign key: they belong to the owner and outlive account or
    message deletion through the snapshots in their sources.
    """

    __tablename__ = "actions"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_actions_public_id"),
        CheckConstraint("status IN ('open','completed')", name="status_known"),
        CheckConstraint(_OWNERSHIP_CHECK, name="ownership_known"),
        CheckConstraint(_PRECISION_CHECK, name="deadline_precision_known"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        Index("ix_actions_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    ownership: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    effort: Mapped[str | None] = mapped_column(String(16))
    deadline_text: Mapped[str | None] = mapped_column(Text)
    deadline_precision: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="none",
        server_default="none",
    )
    deadline_date: Mapped[date | None] = mapped_column(Date)
    deadline_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deadline_timezone: Mapped[str | None] = mapped_column(String(128))
    suggested_target_date: Mapped[date | None] = mapped_column(Date)
    target_reason: Mapped[str | None] = mapped_column(String(32))
    target_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    evidence: Mapped[str | None] = mapped_column(Text)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deleted_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )


class ActionSuggestionTable(Base):
    """One validated action suggested by a cached analysis, at its position."""

    __tablename__ = "action_suggestions"
    __table_args__ = (
        UniqueConstraint("analysis_id", "position", name="uq_action_suggestions_position"),
        UniqueConstraint("analysis_id", "fingerprint", name="uq_action_suggestions_fingerprint"),
        CheckConstraint("position >= 0", name="position_nonnegative"),
        CheckConstraint(_OWNERSHIP_CHECK, name="ownership_known"),
        CheckConstraint(_PRECISION_CHECK, name="deadline_precision_known"),
        # A deleted suggestion's ID is never given to another, so a stale ID fails.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    ownership: Mapped[str] = mapped_column(String(16), nullable=False)
    effort: Mapped[str | None] = mapped_column(String(16))
    deadline_text: Mapped[str | None] = mapped_column(Text)
    deadline_precision: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="none",
        server_default="none",
    )
    deadline_date: Mapped[date | None] = mapped_column(Date)
    deadline_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deadline_timezone: Mapped[str | None] = mapped_column(String(128))
    suggested_target_date: Mapped[date | None] = mapped_column(Date)
    target_reason: Mapped[str | None] = mapped_column(String(32))
    steps_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class ActionStepTable(Base):
    """One ordered step of an accepted action's plan."""

    __tablename__ = "action_steps"
    __table_args__ = (
        CheckConstraint("position >= 0", name="position_nonnegative"),
        Index("ix_action_steps_action_id", "action_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_id: Mapped[int] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    done: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    done_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ActionSourceTable(Base):
    """A snapshot of a message an action came from; it stays when the message goes."""

    __tablename__ = "action_sources"
    __table_args__ = (
        UniqueConstraint("action_id", "provider_message_id", name="uq_action_sources_message"),
        Index("ix_action_sources_message_id", "message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_id: Mapped[int] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    provider_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sender_address: Mapped[str] = mapped_column(String(320), nullable=False)
    web_link: Mapped[str] = mapped_column(Text, nullable=False)
    received_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SuggestionDecisionTable(Base):
    """The owner's decision on one suggestion, kept per provider message and title fingerprint.

    Decisions have no message or account foreign key: they are keyed by the provider
    account and message IDs, so they survive message, cache and account deletion.
    """

    __tablename__ = "suggestion_decisions"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_account_id",
            "provider_message_id",
            "fingerprint",
            name="uq_suggestion_decisions_identity",
        ),
        CheckConstraint("decision IN ('accepted','dismissed')", name="decision_known"),
        Index("ix_suggestion_decisions_action_id", "action_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    action_id: Mapped[int | None] = mapped_column(ForeignKey("actions.id", ondelete="SET NULL"))
    decided_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
