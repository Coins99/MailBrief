"""Normalized email and ranking models."""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, HttpUrl, field_validator, model_validator

from mailbrief.domain.common import DomainModel, normalize_utc


class ProviderKind(StrEnum):
    """Email providers understood by the provider-neutral domain."""

    GMAIL = "gmail"
    MICROSOFT = "microsoft"


class MessageImportance(StrEnum):
    """Normalized provider importance levels."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class RankReason(StrEnum):
    """Auditable inputs to the local ranking score."""

    HIGH_IMPORTANCE = "high_importance"
    LOW_IMPORTANCE = "low_importance"
    UNREAD = "unread"
    DIRECT_RECIPIENT = "direct_recipient"
    HAS_ATTACHMENTS = "has_attachments"
    ACTION_OR_DEADLINE_LANGUAGE = "action_or_deadline_language"
    APPROVAL_OR_REPLY_LANGUAGE = "approval_or_reply_language"
    VERY_RECENT = "very_recent"
    RECENT = "recent"
    AUTOMATED_SENDER = "automated_sender"


class EmailContact(DomainModel):
    """A display name and email address normalized from a provider."""

    name: str | None = Field(default=None, max_length=255)
    address: str = Field(min_length=3, max_length=320)

    @field_validator("address")
    @classmethod
    def validate_address_shape(cls, value: str) -> str:
        """Reject empty local or domain portions without adding a DNS policy."""
        if value.count("@") != 1:
            raise ValueError("email address must contain one @ character")
        local_part, domain = value.rsplit("@", maxsplit=1)
        if not local_part or not domain:
            raise ValueError("email address must contain local and domain portions")
        return value


class AccountIdentity(DomainModel):
    """Provider-neutral identity of a connected mailbox."""

    provider: ProviderKind
    provider_account_id: str = Field(min_length=1, max_length=255)
    email_address: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=255)
    tenant_id: str | None = Field(default=None, max_length=255)

    @field_validator("email_address")
    @classmethod
    def validate_email_address(cls, value: str) -> str:
        """Use the same deliberately small validation boundary as contacts."""
        EmailContact(address=value)
        return value


class NormalizedMessage(DomainModel):
    """Provider message metadata used by storage and local ranking."""

    provider: ProviderKind
    provider_account_id: str = Field(min_length=1, max_length=255)
    provider_message_id: str = Field(min_length=1, max_length=512)
    internet_message_id: str | None = Field(default=None, max_length=998)
    conversation_id: str | None = Field(default=None, max_length=512)
    subject: str = Field(default="", max_length=998)
    sender: EmailContact
    to_recipients: tuple[EmailContact, ...] = ()
    received_at_utc: datetime
    is_read: bool
    is_in_inbox: bool = True
    importance: MessageImportance = MessageImportance.NORMAL
    has_attachments: bool
    body_preview: str = Field(default="", max_length=2_048)
    web_link: HttpUrl

    @field_validator("received_at_utc")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        return normalize_utc(value)


class MessagePage(DomainModel):
    """One normalized provider page and its opaque continuation value."""

    page_number: int = Field(ge=1)
    messages: tuple[NormalizedMessage, ...]
    continuation: str | None = Field(default=None, min_length=1)
    failed_message_count: int = Field(default=0, ge=0)


class RankedMessage(DomainModel):
    """A message paired with its deterministic local score."""

    message: NormalizedMessage
    score: int = Field(ge=-1_000, le=1_000)
    reasons: tuple[RankReason, ...] = ()

    @model_validator(mode="after")
    def require_unique_reasons(self) -> Self:
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError("ranking reasons must be unique")
        return self
