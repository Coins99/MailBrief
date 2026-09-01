"""Unit tests for the Microsoft Graph response mapper."""

from datetime import UTC, datetime

import pytest

from mailbrief.domain.messages import (
    EmailContact,
    MessageImportance,
    ProviderKind,
)
from mailbrief.ports.errors import ProviderResponseError
from mailbrief.providers.microsoft.mapper import (
    map_account_identity,
    map_email_contact,
    map_importance,
    map_message,
    map_message_page,
)


def test_map_account_identity_standard() -> None:
    data = {
        "id": "acc-123",
        "displayName": "Taylor Smith",
        "mail": "taylor@example.com",
        "userPrincipalName": "tsmith@example.com",
    }
    identity = map_account_identity(data, tenant_id="tenant-xyz")

    assert identity.provider is ProviderKind.MICROSOFT
    assert identity.provider_account_id == "acc-123"
    assert identity.email_address == "taylor@example.com"
    assert identity.display_name == "Taylor Smith"
    assert identity.tenant_id == "tenant-xyz"


def test_map_account_identity_fallback_to_upn() -> None:
    data = {
        "id": "acc-456",
        "displayName": "Alex Jones",
        "mail": None,
        "userPrincipalName": "alex@example.com",
    }
    identity = map_account_identity(data)

    assert identity.email_address == "alex@example.com"


def test_map_account_identity_missing_email_raises() -> None:
    data = {"id": "acc-789", "displayName": "No Email", "mail": None, "userPrincipalName": None}
    with pytest.raises(ProviderResponseError, match="missing id or valid email"):
        map_account_identity(data)


def test_map_importance() -> None:
    assert map_importance("high") is MessageImportance.HIGH
    assert map_importance("HIGH") is MessageImportance.HIGH
    assert map_importance("low") is MessageImportance.LOW
    assert map_importance("normal") is MessageImportance.NORMAL
    assert map_importance(None) is MessageImportance.NORMAL
    assert map_importance("unknown") is MessageImportance.NORMAL


def test_map_email_contact() -> None:
    data = {"emailAddress": {"name": "Alice", "address": "alice@example.com"}}
    assert map_email_contact(data) == EmailContact(name="Alice", address="alice@example.com")
    assert map_email_contact({"address": "bob@example.com"}) == EmailContact(
        name=None, address="bob@example.com"
    )
    assert map_email_contact(None) == EmailContact(name=None, address="unknown@example.com")


def test_map_message_standard() -> None:
    data = {
        "id": "msg-123",
        "internetMessageId": "<msg-123@example.com>",
        "conversationId": "conv-456",
        "subject": "Q3 Budget Review",
        "sender": {"emailAddress": {"name": "Jordan", "address": "jordan@example.com"}},
        "toRecipients": [{"emailAddress": {"name": "Taylor", "address": "taylor@example.com"}}],
        "receivedDateTime": "2026-08-31T14:30:00Z",
        "isRead": True,
        "importance": "high",
        "hasAttachments": True,
        "bodyPreview": "Here is the summary.",
        "webLink": "https://outlook.office.com/mail/id/msg-123",
    }

    message = map_message(data, "acc-123")

    assert message.provider is ProviderKind.MICROSOFT
    assert message.provider_account_id == "acc-123"
    assert message.provider_message_id == "msg-123"
    assert message.internet_message_id == "<msg-123@example.com>"
    assert message.conversation_id == "conv-456"
    assert message.subject == "Q3 Budget Review"
    assert message.sender == EmailContact(name="Jordan", address="jordan@example.com")
    assert message.to_recipients == (EmailContact(name="Taylor", address="taylor@example.com"),)
    assert message.received_at_utc == datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
    assert message.is_read is True
    assert message.importance is MessageImportance.HIGH
    assert message.has_attachments is True
    assert message.body_preview == "Here is the summary."
    assert str(message.web_link) == "https://outlook.office.com/mail/id/msg-123"


def test_map_message_missing_required_field_raises() -> None:
    data = {"subject": "Incomplete"}
    with pytest.raises(ProviderResponseError, match="Missing required message field"):
        map_message(data, "acc-123")


def test_map_message_page() -> None:
    data = {
        "value": [
            {
                "id": "msg-1",
                "receivedDateTime": "2026-08-31T10:00:00Z",
                "sender": {"emailAddress": {"name": "A", "address": "a@example.com"}},
                "toRecipients": [],
            }
        ],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=50",
    }

    page = map_message_page(data, "acc-123", page_number=1)

    assert page.page_number == 1
    assert len(page.messages) == 1
    assert page.messages[0].provider_message_id == "msg-1"
    assert page.continuation == "https://graph.microsoft.com/v1.0/me/messages?$skip=50"


def test_map_message_page_invalid_value_raises() -> None:
    with pytest.raises(ProviderResponseError, match="must be a list"):
        map_message_page({"value": "not-a-list"}, "acc-123", 1)
