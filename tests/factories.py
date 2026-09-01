"""Small typed factories shared by unit tests."""

from datetime import UTC, date, datetime

from mailbrief.domain.analysis import AnalysisCategory, MessageAnalysis
from mailbrief.domain.digests import DigestItem, DigestSection
from mailbrief.domain.messages import (
    EmailContact,
    MessageImportance,
    NormalizedMessage,
    ProviderKind,
)


def make_message(**overrides: object) -> NormalizedMessage:
    """Build valid normalized message metadata with optional overrides."""
    values: dict[str, object] = {
        "provider": ProviderKind.MICROSOFT,
        "provider_account_id": "account-1",
        "provider_message_id": "message-1",
        "internet_message_id": "<message-1@example.com>",
        "conversation_id": "conversation-1",
        "subject": "Approval needed by Friday",
        "sender": EmailContact(name="Alex", address="alex@example.com"),
        "to_recipients": (EmailContact(name="Taylor", address="taylor@example.com"),),
        "received_at_utc": datetime(2026, 8, 31, 14, 30, tzinfo=UTC),
        "is_read": False,
        "importance": MessageImportance.HIGH,
        "has_attachments": False,
        "body_preview": "Please approve the attached proposal by Friday.",
        "web_link": "https://outlook.office.com/mail/id/message-1",
    }
    values.update(overrides)
    return NormalizedMessage.model_validate(values)


def make_analysis(**overrides: object) -> MessageAnalysis:
    """Build a valid structured analysis with optional overrides."""
    values: dict[str, object] = {
        "message_key": "local-1",
        "category": AnalysisCategory.ACTION,
        "summary": "Approve the proposal.",
        "action_required": True,
        "action_text": "Approve the proposal by Friday.",
        "deadline_text": "Friday",
        "deadline_at_utc": datetime(2026, 9, 4, 21, 0, tzinfo=UTC),
        "confidence": 0.9,
        "evidence": "Please approve the attached proposal by Friday.",
    }
    values.update(overrides)
    return MessageAnalysis.model_validate(values)


def make_digest_item(**overrides: object) -> DigestItem:
    """Build a valid digest item with optional overrides."""
    values: dict[str, object] = {
        "message_key": "local-1",
        "section": DigestSection.ACTIONS,
        "position": 0,
        "subject": "Approval needed by Friday",
        "sender": EmailContact(name="Alex", address="alex@example.com"),
        "summary": "Approve the proposal.",
        "action_text": "Approve the proposal by Friday.",
        "deadline_at_utc": datetime(2026, 9, 4, 21, 0, tzinfo=UTC),
        "source_url": "https://outlook.office.com/mail/id/message-1",
    }
    values.update(overrides)
    return DigestItem.model_validate(values)


TEST_LOCAL_DATE = date(2026, 8, 31)
TEST_GENERATED_AT = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
