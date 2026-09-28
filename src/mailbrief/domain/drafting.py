"""AI drafting (ADR 0013): the options, exactly what is sent, and what comes back.

A DraftingRequest holds exactly what one generation sends and nothing else: never an email
address, a recipient, another email, an attachment or another draft.
"""

from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from typing import Final, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from mailbrief.domain.actions import ACTION_TITLE_MAX_CHARS
from mailbrief.domain.analysis import ActionOwnership, AIUsage
from mailbrief.domain.common import DomainModel, normalize_utc
from mailbrief.domain.drafts import (
    DRAFT_TITLE_MAX_CHARS,
    Draft,
    DraftGenerationSummary,
    DraftKind,
    DraftLength,
    DraftTone,
)

# Versions carry a generation summary, so these three live with the drafts they describe.
__all__ = ["DraftGenerationSummary", "DraftLength", "DraftTone"]

INSTRUCTIONS_MAX_CHARS: Final = 1_000
ACTION_NOTES_SENT_CHARS: Final = 2_000
CURRENT_TEXT_SENT_CHARS: Final = 8_000
GENERATED_BODY_MAX_CHARS: Final = 8_000
GENERATED_SUBJECT_MAX_CHARS: Final = 200
MAX_MISSING_CONTEXT: Final = 5
MISSING_CONTEXT_MAX_CHARS: Final = 200
COPIED_RUN_LIMIT: Final = 200
# Candidates are unvalidated; these only keep a runaway answer from growing without bound.
_CANDIDATE_TEXT_CHARS: Final = 100_000
_CANDIDATE_ITEMS: Final = 100

_UNTRUSTED = ConfigDict(hide_input_in_errors=True)
# Email bodies and the owner's text are sent as they are, spaces included.
_AS_GIVEN = ConfigDict(hide_input_in_errors=True, str_strip_whitespace=False)


class DraftContextPart(StrEnum):
    """What the owner may choose to send with a generation."""

    SOURCE_EMAIL = "source_email"
    ACTION = "action"
    CURRENT_TEXT = "current_text"


class DraftingProblem(StrEnum):
    """Why an answer could not be used."""

    INCOMPLETE = "incomplete"
    INVALID_OUTPUT = "invalid_output"
    REFUSED = "refused"


class DraftingStatus(StrEnum):
    GENERATED = "generated"
    DECLINED = "declined"
    FAILED = "failed"
    CANCELLED = "cancelled"


PART_ORDER: Final = (
    DraftContextPart.SOURCE_EMAIL,
    DraftContextPart.ACTION,
    DraftContextPart.CURRENT_TEXT,
)


class DraftingOptions(DomainModel):
    """What the owner chose for one generation."""

    model_config = _AS_GIVEN

    parts: frozenset[DraftContextPart] = frozenset()
    tone: DraftTone = DraftTone.NEUTRAL
    length: DraftLength = DraftLength.MEDIUM
    instructions: str = Field(default="", max_length=INSTRUCTIONS_MAX_CHARS, repr=False)


class SourceContext(DomainModel):
    """The email being replied to: the sender's name only, never an address."""

    model_config = _AS_GIVEN

    subject: str = Field(default="", max_length=998, repr=False)
    sender_name: str | None = Field(default=None, max_length=255, repr=False)
    received_local: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, repr=False)
    body_truncated: bool


class ActionContext(DomainModel):
    model_config = _AS_GIVEN

    title: str = Field(min_length=1, max_length=ACTION_TITLE_MAX_CHARS, repr=False)
    ownership: ActionOwnership
    target_date: date | None = None
    deadline_text: str | None = Field(default=None, repr=False)
    steps: tuple[str, ...] = Field(default=(), repr=False)
    notes: str = Field(default="", max_length=ACTION_NOTES_SENT_CHARS, repr=False)


class CurrentText(DomainModel):
    model_config = _AS_GIVEN

    title: str = Field(default="", max_length=DRAFT_TITLE_MAX_CHARS, repr=False)
    body: str = Field(default="", max_length=CURRENT_TEXT_SENT_CHARS, repr=False)


class DraftingRequest(DomainModel):
    """Exactly what one generation sends."""

    model_config = _AS_GIVEN

    kind: DraftKind
    tone: DraftTone
    length: DraftLength
    instructions: str = Field(default="", max_length=INSTRUCTIONS_MAX_CHARS, repr=False)
    today: date
    source: SourceContext | None = None
    action: ActionContext | None = None
    current: CurrentText | None = None

    @property
    def parts(self) -> frozenset[DraftContextPart]:
        chosen = {
            DraftContextPart.SOURCE_EMAIL: self.source is not None,
            DraftContextPart.ACTION: self.action is not None,
            DraftContextPart.CURRENT_TEXT: self.current is not None,
        }
        return frozenset(part for part, present in chosen.items() if present)


def part_sizes(request: DraftingRequest) -> dict[DraftContextPart | None, int]:
    """Characters of text each chosen part sends; None is the part always sent.

    The preview shows these counts, so they are computed from the request itself.
    """
    sizes: dict[DraftContextPart | None, int] = {
        None: len(request.instructions) + len(request.today.isoformat())
    }
    if request.source is not None:
        source = request.source
        sizes[DraftContextPart.SOURCE_EMAIL] = (
            len(source.subject)
            + len(source.sender_name or "")
            + len(source.received_local)
            + len(source.body)
        )
    if request.action is not None:
        action = request.action
        sizes[DraftContextPart.ACTION] = (
            len(action.title)
            + len(action.target_date.isoformat() if action.target_date else "")
            + len(action.deadline_text or "")
            + sum(len(step) for step in action.steps)
            + len(action.notes)
        )
    if request.current is not None:
        sizes[DraftContextPart.CURRENT_TEXT] = len(request.current.title) + len(
            request.current.body
        )
    return sizes


class DraftCandidate(DomainModel):
    """An answer as the adapter parsed it; the drafting service validates it."""

    model_config = _UNTRUSTED

    subject: str | None = Field(default=None, max_length=_CANDIDATE_TEXT_CHARS, repr=False)
    body: str = Field(max_length=_CANDIDATE_TEXT_CHARS, repr=False)
    missing_context: tuple[str, ...] = Field(default=(), max_length=_CANDIDATE_ITEMS, repr=False)


class DraftingResponse(DomainModel):
    """A candidate, or the problem that kept one from being usable."""

    model_config = _UNTRUSTED

    candidate: DraftCandidate | None = None
    problem: DraftingProblem | None = None
    usage: AIUsage | None = None

    @model_validator(mode="after")
    def validate_answer(self) -> Self:
        if (self.candidate is None) == (self.problem is None):
            raise ValueError("a drafting response has a candidate or a problem, not both")
        return self


class PreviewLine(DomainModel):
    label: str = Field(min_length=1)
    characters: int = Field(ge=0)


class DraftingPreview(DomainModel):
    """What the owner approves: the provider, model, notice and each part's size."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    privacy_notice: str = Field(min_length=1)
    first_use: bool
    lines: tuple[PreviewLine, ...]

    @property
    def total_characters(self) -> int:
        return sum(line.characters for line in self.lines)


class GeneratedDraft(DomainModel):
    """A validated answer: cleaned, bounded, with quoted history removed."""

    model_config = _AS_GIVEN

    subject: str | None = Field(
        default=None, min_length=1, max_length=GENERATED_SUBJECT_MAX_CHARS, repr=False
    )
    body: str = Field(min_length=1, max_length=GENERATED_BODY_MAX_CHARS, repr=False)
    missing_context: tuple[str, ...] = Field(default=(), max_length=MAX_MISSING_CONTEXT)

    @field_validator("missing_context")
    @classmethod
    def validate_missing_context(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or len(item) > MISSING_CONTEXT_MAX_CHARS for item in value):
            raise ValueError("missing context items must be 1 to 200 characters")
        return value


class DraftGenerationInfo(DomainModel):
    """The record kept with a generated version."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    tone: DraftTone
    length: DraftLength
    parts: frozenset[DraftContextPart]
    instructions: str = Field(default="", max_length=INSTRUCTIONS_MAX_CHARS, repr=False)
    missing_context: tuple[str, ...] = Field(default=(), max_length=MAX_MISSING_CONTEXT)
    created_at_utc: datetime

    @field_validator("created_at_utc")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return normalize_utc(value)

    def summary(self) -> DraftGenerationSummary:
        return DraftGenerationSummary(tone=self.tone, length=self.length, model=self.model)


class DraftingOutcome(DomainModel):
    """What one generation did. The draft and version numbers are set when it generated.

    ``previous_version`` holds the owner's own text from just before the call.
    """

    status: DraftingStatus
    draft: Draft | None = None
    version_number: int | None = Field(default=None, ge=1)
    previous_version: int | None = Field(default=None, ge=1)
    missing_context: tuple[str, ...] = Field(default=(), max_length=MAX_MISSING_CONTEXT)
    error_code: str | None = None
    provider_detail: str | None = None

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        generated = self.status is DraftingStatus.GENERATED
        if generated != (self.draft is not None and self.version_number is not None):
            raise ValueError("only a generated outcome has a draft and a version")
        if (self.status is DraftingStatus.FAILED) != (self.error_code is not None):
            raise ValueError("only a failed outcome has an error code")
        return self


LENGTH_WORDS: Final[Mapping[DraftLength, str]] = {
    DraftLength.SHORT: "about 80 words at most",
    DraftLength.MEDIUM: "about 80 to 200 words",
    DraftLength.LONG: "about 200 to 400 words",
}
