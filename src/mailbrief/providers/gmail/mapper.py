"""Map Gmail metadata without traversing or retaining MIME bodies."""

import html
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import getaddresses
from urllib.parse import urlencode

from pydantic import HttpUrl, ValidationError

from mailbrief.domain.messages import (
    AccountIdentity,
    EmailContact,
    MessageImportance,
    NormalizedMessage,
)
from mailbrief.ports.errors import ProviderResponseError
from mailbrief.providers.gmail.client import message_id


def clean_text(value: str, limit: int) -> str:
    """Decode encoded headers and remove terminal/control characters."""
    with suppress(ValueError, UnicodeError, LookupError):
        value = str(make_header(decode_header(value)))
    return " ".join("".join(c for c in value if c.isprintable() or c.isspace()).split())[:limit]


def contacts(value: str) -> tuple[EmailContact, ...]:
    result: list[EmailContact] = []
    for name, address in getaddresses([value], strict=False):
        try:
            result.append(
                EmailContact(name=clean_text(name, 255) or None, address=clean_text(address, 320))
            )
        except ValidationError:
            continue
    return tuple(result)


def map_metadata(data: dict[str, object], account: AccountIdentity) -> NormalizedMessage:
    """Fail safely on broken identity/time data, tolerate malformed optional headers."""
    try:
        identifier = message_id(data.get("id"))
        thread = message_id(data.get("threadId"))
        timestamp = data.get("internalDate")
        if not isinstance(timestamp, str) or not timestamp.isascii() or not timestamp.isdigit():
            raise ValueError
        received = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(timestamp))
        labels = data.get("labelIds", [])
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise ValueError
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise ValueError
        headers: dict[str, list[str]] = {}
        raw_headers = payload.get("headers", [])
        if not isinstance(raw_headers, list):
            raise ValueError
        for header in raw_headers:
            if isinstance(header, dict):
                name, value = header.get("name"), header.get("value")
                if isinstance(name, str) and isinstance(value, str):
                    headers.setdefault(name.casefold(), []).append(value[:32_000])
        sender = contacts(", ".join(headers.get("from", [])))
        subject = clean_text(" ".join(headers.get("subject", [])), 998)
        snippet = data.get("snippet", "")
        if not isinstance(snippet, str):
            raise ValueError
        internet_id = clean_text(" ".join(headers.get("message-id", [])), 998) or None
        return NormalizedMessage(
            provider=account.provider,
            provider_account_id=account.provider_account_id,
            provider_message_id=identifier,
            conversation_id=thread,
            internet_message_id=internet_id,
            subject=subject,
            sender=sender[0] if sender else EmailContact(address="unknown@invalid"),
            to_recipients=contacts(", ".join(headers.get("to", []))),
            received_at_utc=received,
            is_read="UNREAD" not in labels,
            is_in_inbox="INBOX" in labels,
            importance=MessageImportance.HIGH
            if "IMPORTANT" in labels
            else MessageImportance.NORMAL,
            # Metadata format does not reliably expose attachment structure; no ranking bonus.
            has_attachments=False,
            body_preview=clean_text(html.unescape(snippet), 2048),
            web_link=HttpUrl(
                "https://mail.google.com/mail/u/?"
                + urlencode({"authuser": account.email_address})
                + "#all/"
                + thread
            ),
        )
    except (ValueError, TypeError, OverflowError):
        raise ProviderResponseError("Gmail message metadata could not be normalized.") from None
