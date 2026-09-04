"""Strict transformations from Microsoft Graph JSON into domain models."""

from collections.abc import Mapping, Sequence
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


def _required_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProviderResponseError("Microsoft Graph returned malformed data.")
    return value


def _optional_str(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProviderResponseError("Microsoft Graph returned malformed data.")
    return value


def _optional_bool(data: Mapping[str, Any], key: str, *, default: bool) -> bool:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ProviderResponseError("Microsoft Graph returned malformed data.")
    return value


def map_account_identity(
    data: Mapping[str, Any],
    *,
    provider_account_id: str | None = None,
    tenant_id: str | None = None,
    extra_addresses: Sequence[str] = (),
) -> AccountIdentity:
    """Map a Graph ``/me`` object without echoing identity data in errors."""
    try:
        account_id = (
            provider_account_id if provider_account_id is not None else _required_str(data, "id")
        )  # noqa: E501
        email = next(
            (
                value
                for key in ("mail", "userPrincipalName")
                if isinstance((value := data.get(key)), str) and value.strip()
            ),
            None,
        )
        if email is None:
            raise ProviderResponseError("Microsoft Graph returned an invalid account profile.")

        seen: set[str] = set()
        account_addresses: list[str] = []
        for candidate in (email, data.get("mail"), data.get("userPrincipalName"), *extra_addresses):
            if isinstance(candidate, str) and candidate.strip():
                folded = candidate.strip().lower()
                if folded not in seen:
                    seen.add(folded)
                    account_addresses.append(candidate.strip())

        display_name = _optional_str(data, "displayName")
        return AccountIdentity(
            provider=ProviderKind.MICROSOFT,
            provider_account_id=account_id,
            email_address=email,
            display_name=display_name,
            tenant_id=tenant_id,
            account_addresses=tuple(account_addresses),
        )
    except ProviderResponseError:
        raise
    except (TypeError, ValueError) as exc:
        raise ProviderResponseError("Microsoft Graph returned an invalid account profile.") from exc


def map_importance(raw: object) -> MessageImportance:
    """Convert the bounded Graph importance vocabulary."""
    if raw is None:
        return MessageImportance.NORMAL
    if not isinstance(raw, str):
        raise ProviderResponseError("Microsoft Graph returned malformed message metadata.")
    try:
        return MessageImportance(raw.casefold())
    except ValueError as exc:
        raise ProviderResponseError("Microsoft Graph returned malformed message metadata.") from exc


def map_email_contact(data: Mapping[str, Any] | None) -> EmailContact:
    """Extract a Graph recipient, reserving the fallback for an absent sender."""
    if data is None:
        return EmailContact(name=None, address="unknown@example.com")
    try:
        info = data.get("emailAddress")
        if not isinstance(info, Mapping):
            raise ProviderResponseError("Microsoft Graph returned malformed contact data.")
        address = _required_str(info, "address").strip()
        name = _optional_str(info, "name")
        return EmailContact(name=name, address=address)
    except ProviderResponseError:
        raise
    except (TypeError, ValueError) as exc:
        raise ProviderResponseError("Microsoft Graph returned malformed contact data.") from exc


def map_message(data: Mapping[str, Any], account_id: str) -> NormalizedMessage:
    """Map one Graph message object with strict field types and nullability."""
    try:
        message_id = _required_str(data, "id")
        received_raw = _required_str(data, "receivedDateTime")
        received_at = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ProviderResponseError("Microsoft Graph returned malformed message metadata.")

        sender_data = data.get("sender")
        if sender_data is None:
            sender_data = data.get("from")
        if sender_data is not None and not isinstance(sender_data, Mapping):
            raise ProviderResponseError("Microsoft Graph returned malformed contact data.")
        sender = map_email_contact(sender_data)

        recipients_data = data.get("toRecipients")
        if not isinstance(recipients_data, list) or not all(
            isinstance(recipient, Mapping) for recipient in recipients_data
        ):
            raise ProviderResponseError("Microsoft Graph returned malformed contact data.")
        recipients = tuple(map_email_contact(recipient) for recipient in recipients_data)

        return NormalizedMessage(
            provider=ProviderKind.MICROSOFT,
            provider_account_id=account_id,
            provider_message_id=message_id,
            internet_message_id=_optional_str(data, "internetMessageId"),
            conversation_id=_optional_str(data, "conversationId"),
            subject=_optional_str(data, "subject") or "",
            sender=sender,
            to_recipients=recipients,
            received_at_utc=received_at,
            is_read=_optional_bool(data, "isRead", default=False),
            importance=map_importance(data.get("importance")),
            has_attachments=_optional_bool(data, "hasAttachments", default=False),
            body_preview=_optional_str(data, "bodyPreview") or "",
            web_link=_required_str(data, "webLink"),
        )
    except ProviderResponseError:
        raise
    except (TypeError, ValueError) as exc:
        raise ProviderResponseError("Microsoft Graph returned malformed message metadata.") from exc


def map_message_page(
    data: Mapping[str, Any],
    account_id: str,
    page_number: int,
) -> MessagePage:
    """Map one Graph page and validate its opaque continuation link."""
    items = data.get("value")
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise ProviderResponseError("Microsoft Graph returned a malformed message page.")

    next_link = data.get("@odata.nextLink")
    if next_link is not None and (not isinstance(next_link, str) or not next_link):
        raise ProviderResponseError("Microsoft Graph returned a malformed message page.")
    try:
        return MessagePage(
            page_number=page_number,
            messages=tuple(map_message(item, account_id) for item in items),
            continuation=next_link,
        )
    except ProviderResponseError:
        raise
    except (TypeError, ValueError) as exc:
        raise ProviderResponseError("Microsoft Graph returned a malformed message page.") from exc
