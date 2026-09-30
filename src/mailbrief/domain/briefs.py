"""Brief-generation contracts shared by the analysis, digest and brief services."""

from datetime import datetime
from enum import StrEnum
from typing import Final, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from mailbrief.domain.bodies import MAX_ANALYSIS_CHARS
from mailbrief.domain.common import DomainModel, normalize_utc
from mailbrief.domain.digests import DailyDigest, DigestCoverage, SyncResult

# The most messages an automatic run may send without asking (ADR 0017).
AUTO_SEND_LIMIT_MAX: Final = 10


class AnalysisOutcome(StrEnum):
    """What happened to one shortlisted message during brief generation."""

    ANALYZED = "analyzed"  # A new provider result from this run.
    REUSED = "reused"  # A valid cached analysis.
    FAILED = "failed"  # A body failure, or no valid result.
    SKIPPED = "skipped"  # An empty or unavailable body; never sent.
    DEFERRED = "deferred"  # Over an automatic run's send limit; never sent or cached.


# Sent for each message before the body; TransmissionPreview.fields adds the body with the
# limit actually applied.
SENT_FIELDS: Final = (
    "subject",
    "sender name and address",
    "received time",
    "your time zone",
)


class TransmissionPreview(DomainModel):
    """What the next provider calls will send, shown before the user consents."""

    provider_name: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    message_count: int = Field(ge=1)  # Messages that will be sent now.
    truncated_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    first_use: bool
    body_character_limit: int = Field(ge=1, le=MAX_ANALYSIS_CHARS)  # The limit applied.
    privacy_notice: str = Field(min_length=1, max_length=500)  # From the AI provider.

    @property
    def fields(self) -> tuple[str, ...]:
        """What is sent for each message, stating the body limit actually applied."""
        body = f"plain-text body, cut to at most {self.body_character_limit:,} characters"
        return (*SENT_FIELDS, body)

    @model_validator(mode="after")
    def validate_truncated_count(self) -> Self:
        if self.truncated_count > self.message_count:
            raise ValueError("truncated_count cannot exceed message_count")
        return self


class AutoSendPermission(DomainModel):
    """How many messages an automatic run may send without asking, on one account's active
    consent (ADR 0017). ``limit`` 0 means none: every analysis asks first."""

    account_email: str = Field(min_length=1, max_length=320)
    limit: int = Field(ge=0, le=AUTO_SEND_LIMIT_MAX)
    granted_at_utc: datetime | None = None

    @field_validator("granted_at_utc")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc(value)


class AutoSendStatus(AutoSendPermission):
    """The permission with the disclosure it would rest on, for the desktop's dialog.

    ``disclosure`` describes what one automatic run would send; its message count is at least
    one, and the dialog sets it to the limit the owner is choosing.
    """

    disclosure: TransmissionPreview


class BriefStatus(StrEnum):
    """Terminal state of one brief-generation run."""

    SAVED = "saved"
    SYNC_FAILED = "sync_failed"
    CANCELLED = "cancelled"
    CONSENT_DECLINED = "consent_declined"
    ANALYSIS_FAILED = "analysis_failed"
    # An automatic run without permission to send: it synced and ranked, nothing more.
    READY_FOR_REVIEW = "ready_for_review"


class BriefRunResult(DomainModel):
    """Outcome of one brief-generation run."""

    model_config = ConfigDict(hide_input_in_errors=True)

    status: BriefStatus
    sync: SyncResult
    digest: DailyDigest | None = None
    coverage: DigestCoverage | None = None
    error_code: str | None = Field(default=None, max_length=128)
    ai_calls: int = Field(default=0, ge=0)  # HTTP requests this run, even failed or retried.
    # The HTTP status and sanitized provider code behind error_code, e.g. "HTTP 403".
    provider_detail: str | None = Field(default=None, max_length=100)
    proposals_created: int = Field(default=0, ge=0)  # Follow-up proposals made (ADR 0016).
    # Messages an automatic run without permission found ready to review; it reads no body.
    ready: int = Field(default=0, ge=0)
    # Messages an automatic run deferred to the next review: over its send limit (ADR 0017).
    deferred: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        if (self.digest is not None) != (self.status is BriefStatus.SAVED):
            raise ValueError("a digest is present exactly when the brief was saved")
        return self
