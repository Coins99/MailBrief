"""Transforms Microsoft Graph JSON payloads into MailBrief domain models."""

from datetime import datetime
from typing import Any

from mailbrief.domain.messages import (
    AccountIdentity,
    EmailContact,
    MessageImportance,
    MessagePage,
    NormalizedMessage,
    ProviderKind,
)
from mailbrief.ports.errors import ProviderResponseError


def map_account_identity(data: dict[str, Any], *, tenant_id: str | None = None) -> AccountIdentity:
    """Map GET /v1.0/me response into AccountIdentity."""
    account_id = data.get("id")
    email = data.get("mail") or data.get("userPrincipalName")

    if not account_id or not email or "@" not in email:
        raise ProviderResponseError(
            f"Invalid Graph /me payload: missing id or valid email (got email={email!r})."
        )

    return AccountIdentity(
        provider=ProviderKind.MICROSOFT,
        provider_account_id=str(account_id),
        email_address=str(email),
        display_name=data.get("displayName"),
        tenant_id=tenant_id,
    )


def map_importance(raw: str | None) -> MessageImportance:
    """Convert Graph importance string to MessageImportance enum."""
    match (raw or "").lower():
        case "high":
            return MessageImportance.HIGH
        case "low":
            return MessageImportance.LOW
        case _:
            return MessageImportance.NORMAL


def map_email_contact(data: dict[str, Any] | None) -> EmailContact:
    """Extract display name and email address from Graph recipient struct."""
    if not data:
        return EmailContact(name=None, address="unknown@example.com")

    info = data.get("emailAddress", data)
    address = str(info.get("address", "")).strip()
    name = info.get("name")

    if not address or "@" not in address:
        address = "unknown@example.com"

    return EmailContact(name=name, address=address)


def map_message(data: dict[str, Any], account_id: str) -> NormalizedMessage:
    """Map single Graph message object into NormalizedMessage."""
    try:
        msg_id = data["id"]
        received_str = data["receivedDateTime"]
        received_dt = datetime.fromisoformat(received_str.replace("Z", "+00:00"))
        web_link = data.get("webLink") or f"https://outlook.office.com/mail/id/{msg_id}"

        sender_data = data.get("sender") or data.get("from")
        sender = map_email_contact(sender_data)

        to_recipients = tuple(
            map_email_contact(rec) for rec in data.get("toRecipients", []) if isinstance(rec, dict)
        )

        return NormalizedMessage(
            provider=ProviderKind.MICROSOFT,
            provider_account_id=account_id,
            provider_message_id=str(msg_id),
            internet_message_id=data.get("internetMessageId"),
            conversation_id=data.get("conversationId"),
            subject=data.get("subject") or "",
            sender=sender,
            to_recipients=to_recipients,
            received_at_utc=received_dt,
            is_read=bool(data.get("isRead", False)),
            importance=map_importance(data.get("importance")),
            has_attachments=bool(data.get("hasAttachments", False)),
            body_preview=data.get("bodyPreview") or "",
            web_link=web_link,
        )
    except KeyError as exc:
        raise ProviderResponseError(f"Missing required message field: {exc}") from exc
    except Exception as exc:
        raise ProviderResponseError(f"Failed to map Graph message: {exc}") from exc


def map_message_page(
    data: dict[str, Any],
    account_id: str,
    page_number: int,
) -> MessagePage:
    """Map Graph messages page into MessagePage."""
    items = data.get("value", [])
    if not isinstance(items, list):
        raise ProviderResponseError("Invalid messages page: 'value' must be a list.")

    messages = tuple(map_message(item, account_id) for item in items)
    next_link = data.get("@odata.nextLink")

    return MessagePage(
        page_number=page_number,
        messages=messages,
        continuation=next_link,
    )
