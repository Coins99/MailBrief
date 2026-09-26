"""Readable message text extracted by a provider; held in memory only, never persisted."""

from enum import StrEnum
from typing import Self

from pydantic import ConfigDict, Field, model_validator

from mailbrief.domain.common import DomainModel

MAX_EXTRACTED_CHARS = 200_000


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
