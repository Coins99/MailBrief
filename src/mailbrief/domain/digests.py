"""Daily digest and synchronization models."""

from datetime import date, datetime
from enum import StrEnum
from typing import Self

from pydantic import ConfigDict, Field, HttpUrl, field_validator, model_validator

from mailbrief.domain.analysis import (
    ACTION_TEXT_MAX_CHARS,
    DEADLINE_TEXT_MAX_CHARS,
    EVIDENCE_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    DeadlinePrecision,
)
from mailbrief.domain.common import DomainModel, normalize_utc
from mailbrief.domain.messages import EmailContact


class DigestSection(StrEnum):
    """Visible sections in a daily brief."""

    HIGHLIGHTS = "highlights"
    ACTIONS = "actions"
    DEADLINES = "deadlines"
    DECISIONS = "decisions"


class DigestStatus(StrEnum):
    """Completion state of a persisted daily brief."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"


class DigestItem(DomainModel):
    """One user-visible item in a daily brief."""

    model_config = ConfigDict(hide_input_in_errors=True)

    message_key: str = Field(min_length=1, max_length=512)
    section: DigestSection
    position: int = Field(ge=0)
    subject: str = Field(default="", max_length=998, repr=False)
    sender: EmailContact
    summary: str = Field(min_length=1, max_length=SUMMARY_MAX_CHARS, repr=False)
    action_text: str | None = Field(default=None, max_length=ACTION_TEXT_MAX_CHARS, repr=False)
    deadline_text: str | None = Field(default=None, max_length=DEADLINE_TEXT_MAX_CHARS, repr=False)
    deadline_precision: DeadlinePrecision = DeadlinePrecision.NONE
    deadline_date: date | None = None
    deadline_at_utc: datetime | None = None
    evidence: str | None = Field(default=None, max_length=EVIDENCE_MAX_CHARS, repr=False)
    source_url: HttpUrl

    @field_validator("deadline_at_utc")
    @classmethod
    def normalize_deadline(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc(value)


class DigestCoverage(DomainModel):
    """How a brief accounts for every shortlisted message.

    - analyzed: new provider results from this run.
    - reused: valid cached analyses.
    - failed: body or analysis failures.
    - skipped: empty or unavailable bodies, which are never sent.
    """

    sync_complete: bool
    shortlisted: int = Field(ge=0)
    analyzed: int = Field(ge=0)
    reused: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    ai_provider: str | None = Field(default=None, max_length=64)
    ai_model: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.analyzed + self.reused + self.failed + self.skipped != self.shortlisted:
            raise ValueError("coverage counts must add up to the shortlist size")
        return self


class DailyDigest(DomainModel):
    """A complete, partial, or empty brief for one local calendar day."""

    model_config = ConfigDict(hide_input_in_errors=True)

    account_id: str = Field(min_length=1, max_length=255)
    local_date: date
    timezone_name: str = Field(min_length=1, max_length=128)
    generated_at_utc: datetime
    status: DigestStatus
    items: tuple[DigestItem, ...] = ()
    coverage: DigestCoverage | None = None  # None only for briefs saved before M4.

    @field_validator("generated_at_utc")
    @classmethod
    def normalize_generated_at(cls, value: datetime) -> datetime:
        return normalize_utc(value)

    @model_validator(mode="after")
    def validate_items(self) -> Self:
        positions = [item.position for item in self.items]
        if len(positions) != len(set(positions)):
            raise ValueError("digest item positions must be unique")
        if positions and sorted(positions) != list(range(len(positions))):
            raise ValueError("digest item positions must be contiguous from zero")

        message_keys = [item.message_key for item in self.items]
        if len(message_keys) != len(set(message_keys)):
            raise ValueError("a message may appear only once in a digest")

        if self.status is DigestStatus.EMPTY and self.items:
            raise ValueError("an empty digest cannot contain items")
        if self.status is not DigestStatus.EMPTY and not self.items:
            raise ValueError("a non-empty digest must contain at least one item")
        return self


class SyncStage(StrEnum):
    """Progress stages emitted by the synchronization workflow."""

    CONNECTING = "connecting"
    FETCHING = "fetching"
    RANKING = "ranking"
    ANALYZING = "analyzing"
    ASSEMBLING = "assembling"


class SyncStatus(StrEnum):
    """Terminal synchronization states."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


class SyncProgress(DomainModel):
    """Provider-neutral progress update suitable for the UI."""

    stage: SyncStage
    pages_fetched: int = Field(default=0, ge=0)
    messages_fetched: int = Field(default=0, ge=0)
    failed_message_count: int = Field(default=0, ge=0)
    ai_batches_completed: int = Field(default=0, ge=0)
    detail: str | None = Field(default=None, max_length=255)


class SyncResult(DomainModel):
    """Terminal result of one synchronization operation."""

    account_id: str = Field(min_length=1, max_length=255)
    range_start_utc: datetime
    range_end_utc: datetime
    status: SyncStatus
    page_count: int = Field(ge=0)
    message_count: int = Field(ge=0)
    failed_message_count: int = Field(default=0, ge=0)
    shortlisted_message_keys: tuple[str, ...] = ()
    error_code: str | None = Field(default=None, max_length=128)

    @field_validator("range_start_utc", "range_end_utc")
    @classmethod
    def normalize_range_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc(value)

    @model_validator(mode="after")
    def validate_range_and_error(self) -> Self:
        if self.range_end_utc <= self.range_start_utc:
            raise ValueError("range_end_utc must be later than range_start_utc")
        if self.status is SyncStatus.FAILED and not self.error_code:
            raise ValueError("a failed sync requires an error_code")
        return self
