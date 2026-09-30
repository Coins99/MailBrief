"""User-owned actions accepted from suggestions, with their steps and source messages."""

from datetime import date, datetime
from enum import StrEnum
from typing import Final, Self
from zoneinfo import ZoneInfo

from pydantic import ConfigDict, Field, HttpUrl, field_validator, model_validator

from mailbrief.domain.analysis import (
    DEADLINE_TEXT_MAX_CHARS,
    SUGGESTION_EVIDENCE_CHARS,
    ActionEffort,
    ActionOwnership,
    ActionSuggestion,
    DeadlinePrecision,
    TargetReason,
    check_deadline_fields,
    deadline_due_at,
)
from mailbrief.domain.common import DomainModel, normalize_utc

ACTION_TITLE_MAX_CHARS: Final = 200
ACTION_STEP_MAX_CHARS: Final = 500
MAX_ACTION_STEPS: Final = 30
ACTION_NOTES_MAX_CHARS: Final = 10_000
PUBLIC_ID_CHARS: Final = 36  # A UUID in its canonical text form.

TARGET_REASON_TEXT: Final[dict[TargetReason, str]] = {
    TargetReason.WORKING_DAY_BEFORE: "one working day before the deadline",
    TargetReason.ON_DEADLINE: "on the deadline; no earlier working day was left",
}


class ActionStatus(StrEnum):
    """Whether an accepted action is still open."""

    OPEN = "open"
    COMPLETED = "completed"


class SuggestionState(StrEnum):
    """What the owner decided about a suggestion."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"


class ActionFilter(StrEnum):
    """Views of the action list."""

    OPEN = "open"
    WAITING = "waiting"
    COMPLETED = "completed"


class ActionStep(DomainModel):
    """One ordered step of an action's plan."""

    model_config = ConfigDict(hide_input_in_errors=True)

    step_id: int = Field(ge=1)
    position: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=ACTION_STEP_MAX_CHARS, repr=False)
    done: bool
    done_at_utc: datetime | None = None

    @field_validator("done_at_utc")
    @classmethod
    def normalize_done_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc(value)

    @model_validator(mode="after")
    def validate_done_at(self) -> Self:
        if self.done_at_utc is not None and not self.done:
            raise ValueError("only a done step can have done_at_utc")
        return self


class ActionSource(DomainModel):
    """A message an action came from; ``available`` says whether it is still cached."""

    model_config = ConfigDict(hide_input_in_errors=True)

    provider_message_id: str = Field(min_length=1, max_length=512)
    subject: str = Field(default="", max_length=998, repr=False)
    sender_address: str = Field(min_length=3, max_length=320)
    web_link: HttpUrl
    received_at_utc: datetime
    available: bool
    in_inbox: bool | None = None  # None when Inbox membership is unknown.

    @field_validator("received_at_utc")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        return normalize_utc(value)


class ThreadActivity(DomainModel):
    """Later messages in an action's threads, derived from cached metadata (ADR 0015).

    ``new_messages``, ``latest_at_utc`` and ``latest_sender`` (a display name, else the
    address) cover other people's messages after the owner's "seen" watermark; the owner's
    own latest reply is reported separately. None of it ever changes the action.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    new_messages: int = Field(default=0, ge=0)
    latest_at_utc: datetime | None = None
    latest_sender: str | None = Field(default=None, min_length=1, max_length=320, repr=False)
    owner_replied_at_utc: datetime | None = None

    @field_validator("latest_at_utc", "owner_replied_at_utc")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc(value)

    @model_validator(mode="after")
    def validate_latest(self) -> Self:
        new = self.new_messages > 0
        if (self.latest_at_utc is not None) != new or (self.latest_sender is not None) != new:
            raise ValueError("the latest message is set exactly when there are new messages")
        return self

    @property
    def unseen(self) -> bool:
        """Whether anything arrived after the watermark, from others or the owner."""
        return self.new_messages > 0 or self.owner_replied_at_utc is not None


class Action(DomainModel):
    """An action the owner accepted and now owns, with its plan and source messages."""

    model_config = ConfigDict(hide_input_in_errors=True)

    public_id: str = Field(min_length=PUBLIC_ID_CHARS, max_length=PUBLIC_ID_CHARS)
    title: str = Field(min_length=1, max_length=ACTION_TITLE_MAX_CHARS, repr=False)
    ownership: ActionOwnership
    status: ActionStatus
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
    target_date: date | None = None  # The owner's choice.
    notes: str = Field(default="", max_length=ACTION_NOTES_MAX_CHARS, repr=False)
    evidence: str | None = Field(
        default=None, min_length=1, max_length=SUGGESTION_EVIDENCE_CHARS, repr=False
    )
    created_at_utc: datetime
    updated_at_utc: datetime
    completed_at_utc: datetime | None = None
    revision: int = Field(ge=1)
    steps: tuple[ActionStep, ...] = Field(default=(), max_length=MAX_ACTION_STEPS)
    sources: tuple[ActionSource, ...] = ()
    thread_seen_until_utc: datetime | None = None  # The owner's "seen" watermark.
    thread: ThreadActivity | None = None  # None when no source has a thread snapshot.

    @field_validator(
        "deadline_at_utc",
        "created_at_utc",
        "updated_at_utc",
        "completed_at_utc",
        "thread_seen_until_utc",
    )
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc(value)

    @model_validator(mode="after")
    def validate_deadline_and_completion(self) -> Self:
        check_deadline_fields(
            self.deadline_text,
            self.deadline_precision,
            self.deadline_date,
            self.deadline_at_utc,
            self.deadline_timezone,
        )
        if (self.completed_at_utc is not None) != (self.status is ActionStatus.COMPLETED):
            raise ValueError("completed_at_utc must be set exactly when the action is completed")
        return self

    def due_at_utc(self) -> datetime | None:
        """When the deadline falls due; see deadline_due_at."""
        return deadline_due_at(
            self.deadline_precision,
            self.deadline_date,
            self.deadline_at_utc,
            self.deadline_timezone,
        )

    def is_overdue(self, now_utc: datetime) -> bool:
        """Whether a dated deadline has passed; an action without one is never overdue."""
        due = self.due_at_utc()
        return due is not None and now_utc >= due

    def carried_over(self, today: date, zone: ZoneInfo) -> bool:
        """Whether the action was created on an earlier local day than ``today``."""
        return self.created_at_utc.astimezone(zone).date() < today


class ThreadLink(DomainModel):
    """A live, open action with a source in an email's thread, in the same account.

    The brief shows it as a continuation and offers to add the email to it; ``is_source``
    says the email is already one of the action's sources.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    public_id: str = Field(min_length=PUBLIC_ID_CHARS, max_length=PUBLIC_ID_CHARS)
    title: str = Field(min_length=1, max_length=ACTION_TITLE_MAX_CHARS, repr=False)
    revision: int = Field(ge=1)
    ownership: ActionOwnership
    is_source: bool


class ActionEdit(DomainModel):
    """The owner's full set of edits to an action's own fields."""

    model_config = ConfigDict(hide_input_in_errors=True)

    title: str = Field(min_length=1, max_length=ACTION_TITLE_MAX_CHARS, repr=False)
    ownership: ActionOwnership
    effort: ActionEffort | None
    target_date: date | None
    notes: str = Field(max_length=ACTION_NOTES_MAX_CHARS, repr=False)


class StepEdit(DomainModel):
    """One step in the owner's edited plan; a step without ``step_id`` is new."""

    model_config = ConfigDict(hide_input_in_errors=True)

    step_id: int | None = Field(ge=1)
    text: str = Field(min_length=1, max_length=ACTION_STEP_MAX_CHARS, repr=False)
    done: bool


class SuggestionView(DomainModel):
    """A stored suggestion with the owner's decision and, once accepted, its action."""

    model_config = ConfigDict(hide_input_in_errors=True)

    suggestion_id: int = Field(ge=1)
    suggestion: ActionSuggestion
    state: SuggestionState
    action_public_id: str | None = Field(
        default=None, min_length=PUBLIC_ID_CHARS, max_length=PUBLIC_ID_CHARS
    )

    @model_validator(mode="after")
    def validate_action_link(self) -> Self:
        if self.action_public_id is not None and self.state is not SuggestionState.ACCEPTED:
            raise ValueError("only an accepted suggestion can link to an action")
        return self
