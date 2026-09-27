"""AI analysis request, provider response and validated result contracts."""

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Annotated, Final, Self
from zoneinfo import ZoneInfo

from pydantic import ConfigDict, Field, field_validator, model_validator

from mailbrief.domain.bodies import MAX_ANALYSIS_CHARS
from mailbrief.domain.common import DomainModel, normalize_utc
from mailbrief.domain.messages import EmailContact

ANALYSIS_SCHEMA_VERSION: Final = "6"  # Bump when validation changes what may be cached.

# Limits shared by the analysis contract, the brief and storage.
SUMMARY_MAX_CHARS: Final = 240
ACTION_TEXT_MAX_CHARS: Final = 1_000
DEADLINE_TEXT_MAX_CHARS: Final = 500
EVIDENCE_MAX_CHARS: Final = 1_000  # Older rows may hold up to this much.
EVIDENCE_STORE_CHARS: Final = 300  # New evidence is stored at most this long.
EVIDENCE_BODY_SHARE: Final = 0.8  # ...and always strictly shorter than this share of the body.
MAX_ANALYSIS_BATCH: Final = 10  # Messages per provider call.

# Limits for the actions suggested for one message.
MAX_SUGGESTIONS: Final = 5
SUGGESTION_TITLE_MAX_CHARS: Final = 120
MAX_SUGGESTION_STEPS: Final = 5
SUGGESTION_STEP_MAX_CHARS: Final = 120
SUGGESTION_EVIDENCE_CHARS: Final = 160
# All evidence stored for one message: its own quote plus its suggestions' quotes.
EVIDENCE_TOTAL_CHARS: Final = 600

_SuggestionStep = Annotated[str, Field(min_length=1, max_length=SUGGESTION_STEP_MAX_CHARS)]


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


class ActionOwnership(StrEnum):
    """Who does an action: the owner, or someone the owner is waiting for."""

    MINE = "mine"
    WAITING_FOR = "waiting_for"


class ActionEffort(StrEnum):
    """Rough effort an action needs."""

    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"


class TargetReason(StrEnum):
    """Why a target date was suggested for an action."""

    WORKING_DAY_BEFORE = "working_day_before"
    ON_DEADLINE = "on_deadline"  # No earlier working day was left.


def check_deadline_fields(
    text: str | None,
    precision: DeadlinePrecision,
    date: date | None,
    at_utc: datetime | None,
    timezone: str | None,
) -> None:
    """Raise ValueError unless the deadline fields agree with their precision.

    A zone must also be loadable, so a model without its own zone validator can use this.
    """
    if timezone is not None:
        _require_time_zone(timezone)
    if (text is None) != (precision is DeadlinePrecision.NONE):
        raise ValueError("deadline_text must be present exactly when a deadline is stated")
    if precision in (DeadlinePrecision.NONE, DeadlinePrecision.UNRESOLVED):
        if date is not None or at_utc is not None or timezone is not None:
            raise ValueError("an absent or unresolved deadline cannot carry a date or zone")
    elif precision is DeadlinePrecision.DATE:
        if date is None or timezone is None or at_utc is not None:
            raise ValueError("a date deadline requires deadline_date and deadline_timezone only")
    else:
        if date is None or at_utc is None or timezone is None:
            raise ValueError("a datetime deadline requires a date, an instant and a zone")
        if at_utc.astimezone(ZoneInfo(timezone)).date() != date:
            raise ValueError("deadline_at_utc must fall on deadline_date in deadline_timezone")


def deadline_due_at(
    precision: DeadlinePrecision,
    date: date | None,
    at_utc: datetime | None,
    timezone: str | None,
) -> datetime | None:
    """When a dated deadline falls due: its instant, or the start of the next day in its zone.

    A date deadline is due at the next local midnight, as UTC; any other deadline has none.
    """
    if precision is DeadlinePrecision.DATETIME:
        return at_utc
    if precision is DeadlinePrecision.DATE and date is not None and timezone is not None:
        next_day = date + timedelta(days=1)
        return datetime.combine(next_day, time.min, tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    return None


class AnalysisRequest(DomainModel):
    """Minimized provider-neutral content supplied for AI analysis.

    ``message_key`` is opaque and local to one request; it is never a provider message ID.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    message_key: str = Field(min_length=1, max_length=64)
    subject: str = Field(default="", max_length=998, repr=False)
    sender: EmailContact
    received_at_utc: datetime
    timezone_name: str = Field(min_length=1, max_length=128)
    body_text: str = Field(min_length=1, max_length=MAX_ANALYSIS_CHARS, repr=False)
    body_truncated: bool = False

    @field_validator("received_at_utc")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        return normalize_utc(value)

    @field_validator("timezone_name")
    @classmethod
    def validate_timezone_name(cls, value: str) -> str:
        return _require_time_zone(value)


class ActionCandidate(DomainModel):
    """One untrusted action suggested by a provider, before the analysis service checks it.

    Fields are typed but unbounded, and every field is required. Text fields stay out of
    the repr because they paraphrase or quote the email.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    title: str = Field(repr=False)
    ownership: ActionOwnership
    effort: ActionEffort | None
    deadline_text: str | None = Field(repr=False)
    deadline_date: str | None  # "YYYY-MM-DD" as returned.
    deadline_time: str | None  # "HH:MM" as returned.
    stated_timezone: str | None  # A zone the email itself states.
    steps: tuple[str, ...] = Field(repr=False)
    evidence: str = Field(repr=False)


class AnalysisCandidate(DomainModel):
    """Untrusted provider output for one request, before the analysis service validates it.

    Fields are typed but unbounded. Every field is required except ``actions``, which
    defaults to none here only; the provider's wire schema still requires it. Text fields
    stay out of the repr because they paraphrase or quote the email.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    message_key: str
    category: AnalysisCategory
    summary: str = Field(repr=False)
    action_required: bool
    action_text: str | None = Field(repr=False)
    deadline_text: str | None = Field(repr=False)
    deadline_date: str | None  # "YYYY-MM-DD" as returned.
    deadline_time: str | None  # "HH:MM" as returned.
    stated_timezone: str | None  # A zone the email itself states.
    confidence: float
    evidence: str = Field(repr=False)
    actions: tuple[ActionCandidate, ...] = ()


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


class ActionSuggestion(DomainModel):
    """A validated action suggested for one message, which the owner may accept or dismiss.

    ``fingerprint`` identifies the title with case, punctuation and spacing ignored, so a
    later analysis of the message cannot repeat a suggestion the owner already decided on.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    position: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=SUGGESTION_TITLE_MAX_CHARS, repr=False)
    ownership: ActionOwnership
    effort: ActionEffort | None = None
    deadline_text: str | None = Field(
        default=None, min_length=1, max_length=DEADLINE_TEXT_MAX_CHARS, repr=False
    )
    deadline_precision: DeadlinePrecision = DeadlinePrecision.NONE
    deadline_date: date | None = None
    deadline_at_utc: datetime | None = None
    deadline_timezone: str | None = None
    suggested_target_date: date | None = None
    target_reason: TargetReason | None = None
    steps: tuple[_SuggestionStep, ...] = Field(
        default=(), max_length=MAX_SUGGESTION_STEPS, repr=False
    )
    evidence: str | None = Field(
        default=None, min_length=1, max_length=SUGGESTION_EVIDENCE_CHARS, repr=False
    )
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("deadline_at_utc")
    @classmethod
    def normalize_deadline(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc(value)

    @field_validator("deadline_timezone")
    @classmethod
    def validate_deadline_timezone(cls, value: str | None) -> str | None:
        return None if value is None else _require_time_zone(value)

    @model_validator(mode="after")
    def validate_deadline_and_target(self) -> Self:
        check_deadline_fields(
            self.deadline_text,
            self.deadline_precision,
            self.deadline_date,
            self.deadline_at_utc,
            self.deadline_timezone,
        )
        if (self.suggested_target_date is None) != (self.target_reason is None):
            raise ValueError("suggested_target_date and target_reason must be set together")
        return self


class MessageAnalysis(DomainModel):
    """Validated structured analysis of one message."""

    model_config = ConfigDict(hide_input_in_errors=True)

    message_key: str = Field(min_length=1, max_length=64)
    category: AnalysisCategory
    summary: str = Field(min_length=1, max_length=SUMMARY_MAX_CHARS, repr=False)
    action_required: bool
    action_text: str | None = Field(
        default=None, min_length=1, max_length=ACTION_TEXT_MAX_CHARS, repr=False
    )
    deadline_text: str | None = Field(
        default=None, min_length=1, max_length=DEADLINE_TEXT_MAX_CHARS, repr=False
    )
    deadline_precision: DeadlinePrecision = DeadlinePrecision.NONE
    deadline_date: date | None = None
    deadline_at_utc: datetime | None = None
    deadline_timezone: str | None = None  # The zone used to resolve the date or instant.
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=1, max_length=EVIDENCE_MAX_CHARS, repr=False)
    suggestions: tuple[ActionSuggestion, ...] = ()

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
        check_deadline_fields(
            self.deadline_text,
            self.deadline_precision,
            self.deadline_date,
            self.deadline_at_utc,
            self.deadline_timezone,
        )
        self._validate_suggestions()
        return self

    def _validate_suggestions(self) -> None:
        if len(self.suggestions) > MAX_SUGGESTIONS:
            raise ValueError(f"a message has at most {MAX_SUGGESTIONS} suggestions")
        if [item.position for item in self.suggestions] != list(range(len(self.suggestions))):
            raise ValueError("suggestion positions must run from zero in order")
        if len({item.fingerprint for item in self.suggestions}) != len(self.suggestions):
            raise ValueError("suggestion fingerprints must be unique within a message")
