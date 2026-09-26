"""Brief-generation contracts shared by the analysis, digest and brief services."""

from enum import StrEnum
from typing import Final, Self

from pydantic import ConfigDict, Field, model_validator

from mailbrief.domain.bodies import MAX_ANALYSIS_CHARS
from mailbrief.domain.common import DomainModel
from mailbrief.domain.digests import DailyDigest, DigestCoverage, SyncResult


class AnalysisOutcome(StrEnum):
    """What happened to one shortlisted message during brief generation."""

    ANALYZED = "analyzed"  # A new provider result from this run.
    REUSED = "reused"  # A valid cached analysis.
    FAILED = "failed"  # A body failure, or no valid result.
    SKIPPED = "skipped"  # An empty or unavailable body; never sent.


SENT_FIELDS: Final = (
    "subject",
    "sender name and address",
    "received time",
    "your time zone",
    f"plain-text body, cut to at most {MAX_ANALYSIS_CHARS:,} characters",
)


class TransmissionPreview(DomainModel):
    """What the next provider calls will send, shown before the user consents."""

    provider_name: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    message_count: int = Field(ge=1)  # Messages that will be sent now.
    truncated_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    first_use: bool
    fields: tuple[str, ...] = SENT_FIELDS

    @model_validator(mode="after")
    def validate_truncated_count(self) -> Self:
        if self.truncated_count > self.message_count:
            raise ValueError("truncated_count cannot exceed message_count")
        return self


class BriefStatus(StrEnum):
    """Terminal state of one brief-generation run."""

    SAVED = "saved"
    SYNC_FAILED = "sync_failed"
    CANCELLED = "cancelled"
    CONSENT_DECLINED = "consent_declined"
    ANALYSIS_FAILED = "analysis_failed"


class BriefRunResult(DomainModel):
    """Outcome of one brief-generation run."""

    model_config = ConfigDict(hide_input_in_errors=True)

    status: BriefStatus
    sync: SyncResult
    digest: DailyDigest | None = None
    coverage: DigestCoverage | None = None
    ai_calls: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        if (self.digest is not None) != (self.status is BriefStatus.SAVED):
            raise ValueError("a digest is present exactly when the brief was saved")
        return self
