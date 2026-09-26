"""Readable message text extracted by a provider; held in memory only, never persisted."""

from enum import StrEnum
from typing import Self

from pydantic import ConfigDict, Field, model_validator

from mailbrief.domain.common import DomainModel

MAX_EXTRACTED_CHARS = 200_000
MAX_ANALYSIS_CHARS = 8_000


class BodySource(StrEnum):
    """Where the readable text came from."""

    PLAIN = "plain"
    HTML = "html"
    NONE = "none"


class MessageBody(DomainModel):
    """Readable text of one message. Never store it, log it or put it in an exception."""

    model_config = ConfigDict(hide_input_in_errors=True)

    provider_message_id: str = Field(min_length=1, max_length=512)
    text: str = Field(default="", max_length=MAX_EXTRACTED_CHARS, repr=False)
    source: BodySource
    attachments_skipped: int = Field(default=0, ge=0)
    unreadable_parts: int = Field(default=0, ge=0)
    extraction_truncated: bool = False

    @model_validator(mode="after")
    def text_matches_source(self) -> Self:
        if (self.source is BodySource.NONE) != (not self.text):
            raise ValueError("a body has text exactly when its source is not 'none'")
        return self


class BodyStatus(StrEnum):
    """Outcome of preparing one shortlisted message."""

    READY = "ready"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class PreparedBody(DomainModel):
    """Bounded analysis text for one message. In memory only; never store, log or print it."""

    model_config = ConfigDict(hide_input_in_errors=True)

    provider_message_id: str = Field(min_length=1, max_length=512)
    status: BodyStatus
    text: str = Field(default="", max_length=MAX_ANALYSIS_CHARS, repr=False)
    source: BodySource = BodySource.NONE
    original_chars: int = Field(default=0, ge=0)
    quoted_history_removed: bool = False
    truncated: bool = False
    attachments_skipped: int = Field(default=0, ge=0)
    unreadable_parts: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def text_only_when_ready(self) -> Self:
        if (self.status is BodyStatus.READY) != bool(self.text):
            raise ValueError("prepared text is present exactly when the status is 'ready'")
        return self
