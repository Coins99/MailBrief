"""AI analysis request and result contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from mailbrief.domain.common import DomainModel, normalize_utc
from mailbrief.domain.messages import EmailContact


class AnalysisCategory(StrEnum):
    """Digest categories returned by an AI provider."""

    ACTION = "action"
    DEADLINE = "deadline"
    DECISION = "decision"
    INFORMATION = "information"


class AnalysisRequest(DomainModel):
    """Minimized provider-neutral content supplied for AI analysis."""

    message_key: str = Field(min_length=1, max_length=64)
    subject: str = Field(default="", max_length=998)
    sender: EmailContact
    received_at_utc: datetime
    body_text: str = Field(min_length=1, max_length=8_000)

    @field_validator("received_at_utc")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        return normalize_utc(value)


class MessageAnalysis(DomainModel):
    """Validated structured analysis returned by an AI provider."""

    message_key: str = Field(min_length=1, max_length=64)
    category: AnalysisCategory
    summary: str = Field(min_length=1, max_length=240)
    action_required: bool
    action_text: str | None = Field(default=None, max_length=1_000)
    deadline_text: str | None = Field(default=None, max_length=500)
    deadline_at_utc: datetime | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=1, max_length=1_000)

    @field_validator("deadline_at_utc")
    @classmethod
    def normalize_deadline(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc(value)

    @model_validator(mode="after")
    def validate_conditional_fields(self) -> Self:
        if self.action_required and not self.action_text:
            raise ValueError("action_text is required when action_required is true")
        if self.category is AnalysisCategory.DEADLINE and not (
            self.deadline_text or self.deadline_at_utc
        ):
            raise ValueError("a deadline analysis requires deadline_text or deadline_at_utc")
        return self
