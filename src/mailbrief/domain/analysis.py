"""AI analysis request, provider response and validated result contracts."""

from datetime import date, datetime
from enum import StrEnum
from typing import Final, Self
from zoneinfo import ZoneInfo

from pydantic import ConfigDict, Field, field_validator, model_validator

from mailbrief.domain.bodies import MAX_ANALYSIS_CHARS
from mailbrief.domain.common import DomainModel, normalize_utc
from mailbrief.domain.messages import EmailContact

ANALYSIS_SCHEMA_VERSION: Final = "2"


def _require_time_zone(value: str) -> str:
    """Accept only IANA names that ZoneInfo can load; the error never echoes the value."""
    try:
        ZoneInfo(value)
    except (KeyError, ValueError, OSError):
        raise ValueError("time zone must be a loadable IANA name") from None
    return value


class AnalysisCategory(StrEnum):
    """Digest categories returned by an AI provider."""

    ACTION = "action"
    DEADLINE = "deadline"
    DECISION = "decision"
    INFORMATION = "information"


class DeadlinePrecision(StrEnum):
    """How precisely a message states its deadline."""

    NONE = "none"
    UNRESOLVED = "unresolved"  # A phrase with no specific day, for example "ASAP".
    DATE = "date"  # A calendar day without a time.
    DATETIME = "datetime"  # An exact instant.


class AnalysisRequest(DomainModel):
    """Minimized provider-neutral content supplied for AI analysis.

    ``message_key`` is opaque and local to one request; it is never a provider message ID.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    message_key: str = Field(min_length=1, max_length=64)
    subject: str = Field(default="", max_length=998)
    sender: EmailContact
    received_at_utc: datetime
    timezone_name: str = Field(min_length=1, max_length=128)
    body_text: str = Field(min_length=1, max_length=MAX_ANALYSIS_CHARS)
    body_truncated: bool = False

    @field_validator("received_at_utc")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        return normalize_utc(value)

    @field_validator("timezone_name")
    @classmethod
    def validate_timezone_name(cls, value: str) -> str:
        return _require_time_zone(value)


class AnalysisCandidate(DomainModel):
    """Untrusted provider output for one request, before the analysis service validates it.

    Fields are typed but unbounded, and every field is required.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    message_key: str
    category: AnalysisCategory
    summary: str
    action_required: bool
    action_text: str | None
    deadline_text: str | None
    deadline_date: str | None  # "YYYY-MM-DD" as returned.
    deadline_time: str | None  # "HH:MM" as returned.
    stated_timezone: str | None  # A zone the email itself states.
    confidence: float
    evidence: str


class AnalysisProblem(StrEnum):
    """Why a provider call returned no candidates."""

    REFUSED = "refused"
    INCOMPLETE = "incomplete"
    INVALID_OUTPUT = "invalid_output"


class AIUsage(DomainModel):
    """Token counts reported by the provider, when available."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class AnalysisResponse(DomainModel):
    """Candidates from one provider call, or the problem that explains their absence."""

    model_config = ConfigDict(hide_input_in_errors=True)

    candidates: tuple[AnalysisCandidate, ...] = ()
    problem: AnalysisProblem | None = None
    usage: AIUsage | None = None

    @model_validator(mode="after")
    def validate_problem(self) -> Self:
        if self.problem is not None and self.candidates:
            raise ValueError("a response with a problem cannot contain candidates")
        return self


class MessageAnalysis(DomainModel):
    """Validated structured analysis of one message."""

    model_config = ConfigDict(hide_input_in_errors=True)

    message_key: str = Field(min_length=1, max_length=64)
    category: AnalysisCategory
    summary: str = Field(min_length=1, max_length=240)
    action_required: bool
    action_text: str | None = Field(default=None, max_length=1_000)
    deadline_text: str | None = Field(default=None, max_length=500)
    deadline_precision: DeadlinePrecision = DeadlinePrecision.NONE
    deadline_date: date | None = None
    deadline_at_utc: datetime | None = None
    deadline_timezone: str | None = None  # The zone used to resolve the date or instant.
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=1, max_length=1_000)

    @field_validator("deadline_at_utc")
    @classmethod
    def normalize_deadline(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc(value)

    @field_validator("deadline_timezone")
    @classmethod
    def validate_deadline_timezone(cls, value: str | None) -> str | None:
        return None if value is None else _require_time_zone(value)

    @model_validator(mode="after")
    def validate_conditional_fields(self) -> Self:
        if self.action_required and not self.action_text:
            raise ValueError("action_text is required when action_required is true")
        if self.category is AnalysisCategory.DEADLINE and not self.deadline_text:
            raise ValueError("a deadline analysis requires deadline_text")
        self._validate_deadline()
        return self

    def _validate_deadline(self) -> None:
        precision = self.deadline_precision
        if (self.deadline_text is None) != (precision is DeadlinePrecision.NONE):
            raise ValueError("deadline_text must be present exactly when a deadline is stated")
        if precision in (DeadlinePrecision.NONE, DeadlinePrecision.UNRESOLVED):
            if (
                self.deadline_date is not None
                or self.deadline_at_utc is not None
                or self.deadline_timezone is not None
            ):
                raise ValueError("an absent or unresolved deadline cannot carry a date or zone")
        elif precision is DeadlinePrecision.DATE:
            if (
                self.deadline_date is None
                or self.deadline_timezone is None
                or self.deadline_at_utc is not None
            ):
                raise ValueError(
                    "a date deadline requires deadline_date and deadline_timezone only"
                )
        else:
            if (
                self.deadline_date is None
                or self.deadline_at_utc is None
                or self.deadline_timezone is None
            ):
                raise ValueError("a datetime deadline requires a date, an instant and a zone")
            local = self.deadline_at_utc.astimezone(ZoneInfo(self.deadline_timezone))
            if local.date() != self.deadline_date:
                raise ValueError("deadline_at_utc must fall on deadline_date in deadline_timezone")
