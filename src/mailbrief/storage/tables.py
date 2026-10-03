"""SQLAlchemy table mappings for local MailBrief state."""

from datetime import date, datetime

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

from mailbrief.domain.analysis import MAX_ANALYSIS_BATCH
from mailbrief.domain.briefs import AUTO_SEND_LIMIT_MAX
from mailbrief.domain.common import utc_now
from mailbrief.domain.preferences import (
    AI_BODY_CHARS_MAX,
    AI_OUTPUT_TOKENS_MAX,
    AI_OUTPUT_TOKENS_MIN,
    AI_REQUESTS_MAX,
    AI_TIMEOUT_MAX,
    AI_TIMEOUT_MIN,
    REFRESH_INTERVALS,
    SHORTLIST_LIMIT_MAX,
)
from mailbrief.storage.types import UTCDateTime

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_PRECISION_CHECK = "deadline_precision IN ('none','unresolved','date','datetime')"
_OWNERSHIP_CHECK = "ownership IN ('mine','waiting_for')"
_DRAFT_KIND_CHECK = "kind IN ('reply','email','note','message')"
_DRAFT_ORIGIN_CHECK = "origin IN ('created','edited','restored','generated')"
_DRAFT_TONE_CHECK = "tone IN ('neutral','warm','formal','direct')"
_DRAFT_LENGTH_CHECK = "length IN ('short','medium','long')"
_DEFAULT_TONE_CHECK = "draft_tone IN ('neutral','warm','formal','direct')"
_DEFAULT_LENGTH_CHECK = "draft_length IN ('short','medium','long')"
_FOLLOW_UP_CHECK = "follow_up_kind IN ('none','new_deadline','cancelled','delivered')"
_PROPOSAL_KIND_CHECK = "kind IN ('new_deadline','cancelled','delivered')"
_PROPOSAL_STATE_CHECK = "state IN ('pending','applied','dismissed')"
_REFRESH_INTERVAL_CHECK = (
    "refresh_interval_minutes IS NULL OR refresh_interval_minutes IN "
    f"({', '.join(str(minutes) for minutes in REFRESH_INTERVALS)})"
)


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
        Index("ix_messages_account_conversation", "account_id", "conversation_id"),
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
    # Sent from this account; cached only inside tracked threads (ADR 0015).
    is_sent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # When the owner unchecked this message in a review that had picked it (ADR 0017).
    review_declined_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())
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
        CheckConstraint(_FOLLOW_UP_CHECK, name="follow_up_kind_known"),
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
    # The follow-up signal (ADR 0016); rows from before schema 7 read "none".
    follow_up_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none"
    )
    follow_up_evidence: Mapped[str | None] = mapped_column(String(160))


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
    # Messages an automatic run left for the next review (ADR 0017); 0 for older briefs.
    deferred_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
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
        CheckConstraint(
            f"auto_send_limit BETWEEN 0 AND {AUTO_SEND_LIMIT_MAX}", name="auto_send_limit_range"
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
    # How many messages an automatic run may send without asking, and since when (ADR 0017).
    # It belongs to this consent: revoking the consent, or a new disclosure version, ends it.
    auto_send_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    auto_send_granted_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())


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
    # The owner's "seen" watermark for later messages in the action's threads (ADR 0015).
    thread_seen_until_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())

    # Every UPDATE and DELETE carries "WHERE revision = <the revision this session loaded>",
    # so a writer holding a stale row changes nothing and the ORM raises StaleDataError
    # instead of silently overwriting a newer change. Services still set the next revision.
    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}


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
    # The message's provider, account and thread, so its thread can be tracked (ADR 0015).
    # NULL for sources whose email had left local mail before revision 0010.
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_account_id: Mapped[str | None] = mapped_column(String(255))
    provider_thread_id: Mapped[str | None] = mapped_column(String(512))


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


class ActionProposalTable(Base):
    """A later email's proposed update to an action, applied only by the owner (ADR 0016).

    It keeps a snapshot of the email, so it outlives the cached message. ``previous_json``,
    ``source_added`` and ``applied_revision`` record what applying changed, for Undo.
    """

    __tablename__ = "action_proposals"
    __table_args__ = (
        UniqueConstraint(
            "action_id", "provider_message_id", "kind", name="uq_action_proposals_identity"
        ),
        CheckConstraint(_PROPOSAL_KIND_CHECK, name="kind_known"),
        CheckConstraint(_PROPOSAL_STATE_CHECK, name="state_known"),
        CheckConstraint(_PRECISION_CHECK, name="deadline_precision_known"),
        Index("ix_action_proposals_action_id", "action_id"),
        # A deleted proposal's ID is never given to another, so a stale ID fails.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_id: Mapped[int] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
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
    evidence: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    provider_thread_id: Mapped[str | None] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sender_address: Mapped[str] = mapped_column(String(320), nullable=False)
    web_link: Mapped[str] = mapped_column(Text, nullable=False)
    received_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    previous_json: Mapped[dict[str, str | None] | None] = mapped_column(JSON)
    source_added: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    applied_revision: Mapped[int | None] = mapped_column(Integer)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decided_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())


class DraftTable(Base):
    """A draft or note the owner is writing (ADR 0012).

    Drafts have no account foreign key: they belong to the owner. Their action link is
    SET NULL, and the action's title is kept as a snapshot, so they outlive the action.
    """

    __tablename__ = "drafts"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_drafts_public_id"),
        CheckConstraint(_DRAFT_KIND_CHECK, name="kind_known"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        Index("ix_drafts_updated_at_utc", "updated_at_utc"),
        Index("ix_drafts_action_id", "action_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    to_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    cc_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    action_id: Mapped[int | None] = mapped_column(ForeignKey("actions.id", ondelete="SET NULL"))
    action_title: Mapped[str | None] = mapped_column(Text)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    deleted_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    # Every UPDATE and DELETE carries "WHERE revision = <the revision this session loaded>",
    # so a writer holding a stale row changes nothing and the ORM raises StaleDataError
    # instead of silently overwriting a newer change. Services still set the next revision.
    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}


class DraftVersionTable(Base):
    """One saved version of a draft's text; numbers only grow, so pruning leaves gaps."""

    __tablename__ = "draft_versions"
    __table_args__ = (
        UniqueConstraint("draft_id", "number", name="uq_draft_versions_number"),
        CheckConstraint("number >= 1", name="number_positive"),
        CheckConstraint(_DRAFT_ORIGIN_CHECK, name="origin_known"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"),
        nullable=False,
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    to_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    cc_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DraftSourceTable(Base):
    """A snapshot of an email a draft came from; it stays when the message goes."""

    __tablename__ = "draft_sources"
    __table_args__ = (
        UniqueConstraint("draft_id", "provider_message_id", name="uq_draft_sources_message"),
        Index("ix_draft_sources_message_id", "message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    provider_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sender_address: Mapped[str] = mapped_column(String(320), nullable=False)
    web_link: Mapped[str] = mapped_column(Text, nullable=False)
    received_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class OwnerConsentTable(Base):
    """The owner's consent to one use of an AI provider, such as drafting (ADR 0013).

    It mirrors ``ai_consents`` without an account: drafting consent belongs to the owner.
    A consent is active until it is revoked.
    """

    __tablename__ = "owner_consents"
    __table_args__ = (
        UniqueConstraint("provider", "scope", "disclosure_version", name="uq_owner_consents_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    disclosure_version: Mapped[str] = mapped_column(String(16), nullable=False)
    granted_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime())


class DraftGenerationTable(Base):
    """How a generated draft version was made; it goes when its version is pruned."""

    __tablename__ = "draft_generations"
    __table_args__ = (
        UniqueConstraint("version_id", name="uq_draft_generations_version"),
        CheckConstraint(_DRAFT_TONE_CHECK, name="tone_known"),
        CheckConstraint(_DRAFT_LENGTH_CHECK, name="length_known"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("draft_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tone: Mapped[str] = mapped_column(String(16), nullable=False)
    length: Mapped[str] = mapped_column(String(16), nullable=False)
    parts_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    missing_context_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


def _range_check(column: str, low: int, high: int) -> CheckConstraint:
    return CheckConstraint(
        f"{column} IS NULL OR {column} BETWEEN {low} AND {high}", name=f"{column}_range"
    )


class OwnerPreferencesTable(Base):
    """The owner's preferences: one revisioned row, shared by the desktop and CLI (ADR 0014).

    No row means the defaults. A NULL AI limit means "use the default".
    """

    __tablename__ = "owner_preferences"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint(
            f"shortlist_limit BETWEEN 1 AND {SHORTLIST_LIMIT_MAX}", name="shortlist_limit_range"
        ),
        CheckConstraint(_DEFAULT_TONE_CHECK, name="draft_tone_known"),
        CheckConstraint(_DEFAULT_LENGTH_CHECK, name="draft_length_known"),
        CheckConstraint(_REFRESH_INTERVAL_CHECK, name="refresh_interval_known"),
        _range_check("ai_batch_size", 1, MAX_ANALYSIS_BATCH),
        _range_check("ai_body_character_limit", 1, AI_BODY_CHARS_MAX),
        _range_check("ai_max_output_tokens", AI_OUTPUT_TOKENS_MIN, AI_OUTPUT_TOKENS_MAX),
        _range_check("ai_max_requests_per_run", 1, AI_REQUESTS_MAX),
        _range_check("ai_timeout_seconds", AI_TIMEOUT_MIN, AI_TIMEOUT_MAX),
        CheckConstraint("revision >= 1", name="revision_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    time_zone: Mapped[str | None] = mapped_column(String(64))
    shortlist_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=SHORTLIST_LIMIT_MAX, server_default=text("10")
    )
    excluded_senders_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    draft_tone: Mapped[str] = mapped_column(
        String(16), nullable=False, default="neutral", server_default="neutral"
    )
    draft_length: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium", server_default="medium"
    )
    ai_batch_size: Mapped[int | None] = mapped_column(Integer)
    ai_body_character_limit: Mapped[int | None] = mapped_column(Integer)
    ai_max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    ai_max_requests_per_run: Mapped[int | None] = mapped_column(Integer)
    ai_timeout_seconds: Mapped[float | None] = mapped_column(Float)
    # Automatic refresh while the desktop is open (ADR 0017): on launch, and every N minutes.
    refresh_on_launch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    refresh_interval_minutes: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    updated_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
