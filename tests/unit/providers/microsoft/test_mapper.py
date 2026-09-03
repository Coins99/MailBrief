"""Unit tests for strict Microsoft Graph response mapping."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from mailbrief.domain.messages import EmailContact, MessageImportance, ProviderKind
from mailbrief.ports.errors import ProviderResponseError
from mailbrief.providers.microsoft.mapper import (
    map_account_identity,
    map_email_contact,
    map_importance,
    map_message,
    map_message_page,
)


def _message() -> dict[str, object]:
    return {
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


def test_map_account_identity_uses_mail_then_upn() -> None:
    identity = map_account_identity(
        {
            "id": "account",
            "mail": "mail@example.com",
            "userPrincipalName": "upn@example.com",
            "displayName": "Taylor",
        },
        tenant_id="tenant",
    )
    assert identity.provider is ProviderKind.MICROSOFT
    assert identity.email_address == "mail@example.com"
    assert identity.tenant_id == "tenant"

    fallback = map_account_identity(
        {"id": "account", "mail": 42, "userPrincipalName": "upn@example.com"}
    )
    assert fallback.email_address == "upn@example.com"


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "account", "mail": None},
        {"id": 7, "mail": "private@example.com"},
        {"id": "account", "mail": "private@example.com", "displayName": 7},
    ],
)
def test_invalid_account_payload_is_sanitized(payload: dict[str, object]) -> None:
    with pytest.raises(ProviderResponseError) as exc_info:
        map_account_identity(payload)
    assert "private@example.com" not in str(exc_info.value)


def test_importance_is_bounded_and_case_insensitive() -> None:
    assert map_importance("HIGH") is MessageImportance.HIGH
    assert map_importance("normal") is MessageImportance.NORMAL
    assert map_importance(None) is MessageImportance.NORMAL
    for invalid in ("unknown", False, 1):
        with pytest.raises(ProviderResponseError):
            map_importance(invalid)


def test_contact_requires_graph_shape_but_absent_sender_has_fallback() -> None:
    contact = map_email_contact({"emailAddress": {"name": "Alice", "address": "alice@example.com"}})
    assert contact == EmailContact(name="Alice", address="alice@example.com")
    assert map_email_contact(None) == EmailContact(name=None, address="unknown@example.com")
    invalid_contacts: tuple[dict[str, object], ...] = (
        {"address": "bob@example.com"},
        {"emailAddress": []},
    )
    for invalid in invalid_contacts:
        with pytest.raises(ProviderResponseError):
            map_email_contact(invalid)


def test_map_message_maps_all_selected_fields() -> None:
    message = map_message(_message(), "account")
    assert message.provider_message_id == "msg-123"
    assert message.subject == "Q3 Budget Review"
    assert message.received_at_utc == datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
    assert message.is_read is True
    assert message.has_attachments is True
    assert message.importance is MessageImportance.HIGH
    assert message.to_recipients == (EmailContact(name="Taylor", address="taylor@example.com"),)


def test_missing_sender_uses_documented_fallback() -> None:
    payload = _message()
    del payload["sender"]
    assert map_message(payload, "account").sender.address == "unknown@example.com"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 1),
        ("receivedDateTime", "2026-08-31T14:30:00"),
        ("webLink", None),
        ("toRecipients", {}),
        ("toRecipients", ["not-a-recipient"]),
        ("sender", "not-a-sender"),
        ("isRead", "false"),
        ("hasAttachments", 1),
        ("importance", "urgent"),
        ("subject", 1),
        ("bodyPreview", False),
        ("internetMessageId", 1),
    ],
)
def test_malformed_present_message_values_are_rejected(field: str, value: object) -> None:
    payload = deepcopy(_message())
    payload[field] = value
    with pytest.raises(ProviderResponseError):
        map_message(payload, "account")


def test_message_page_requires_mapping_items_and_valid_continuation() -> None:
    page = map_message_page(
        {
            "value": [_message()],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=50",
        },
        "account",
        1,
    )
    assert len(page.messages) == 1
    assert page.page_number == 1

    for payload in (
        {},
        {"value": "not-a-list"},
        {"value": ["not-an-object"]},
        {"value": [], "@odata.nextLink": ""},
        {"value": [], "@odata.nextLink": 1},
    ):
        with pytest.raises(ProviderResponseError):
            map_message_page(payload, "account", 1)
