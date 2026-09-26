"""Readable text from a Gmail format=full message, without downloading attachments.

Gmail's format=full returns each text part already transfer-decoded, and normally as UTF-8
even when the part's header still names another charset. Parts are therefore decoded as
UTF-8 first; the declared charset is only a fallback for bytes that are not valid UTF-8.
Quoted-printable decoding is never applied again.
"""

import base64
import binascii
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.message import Message

from mailbrief.domain.bodies import MAX_EXTRACTED_CHARS, BodySource, MessageBody
from mailbrief.ports.errors import ProviderResponseError
from mailbrief.text.html_to_text import html_to_text

MAX_SEPARATE_PART_BYTES = 1_000_000
# Last-resort bound per decoded part. Responses are already capped at 2 MB and separately
# stored parts at 1 MB, so this rarely applies; any cut sets extraction_truncated.
MAX_PART_CHARS = 2_000_000
MAX_PARTS = 200
MAX_DEPTH = 20
STUB_PLAIN_CHARS = 400

PartFetcher = Callable[[str], Awaitable[dict[str, object] | None]]


@dataclass
class _Segment:
    """One piece of readable content: a single text part or a set of alternatives."""

    plain: list[dict[str, object]] = field(default_factory=list)
    html: list[dict[str, object]] = field(default_factory=list)


@dataclass
class _Counts:
    parts: int = 0
    attachments: int = 0
    unreadable: int = 0
    truncated: bool = False


def _header(part: dict[str, object], name: str) -> str:
    headers = part.get("headers")
    if isinstance(headers, list):
        for item in headers:
            if isinstance(item, dict) and str(item.get("name", "")).casefold() == name:
                value = item.get("value")
                if isinstance(value, str):
                    return value
    return ""


def _mime(part: dict[str, object]) -> str:
    value = part.get("mimeType")
    return value.casefold() if isinstance(value, str) else ""


def _is_attachment(part: dict[str, object]) -> bool:
    filename = part.get("filename")
    if isinstance(filename, str) and filename.strip():
        return True
    return _header(part, "content-disposition").strip().casefold().startswith("attachment")


def _has_content(part: dict[str, object]) -> bool:
    body = part.get("body")
    if not isinstance(body, dict):
        return False
    size = body.get("size")
    return bool(body.get("attachmentId")) or (isinstance(size, int) and size > 0)


def _charset(part: dict[str, object]) -> str | None:
    header = _header(part, "content-type")
    if not header:
        return None
    message = Message()
    message["content-type"] = header
    return message.get_content_charset()


def _decode(raw: bytes, charset: str | None) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if charset:
        try:
            return raw.decode(charset, errors="replace")
        except (LookupError, UnicodeError):
            pass
    return raw.decode("utf-8", errors="replace")


def _collect(part: dict[str, object], counts: _Counts, depth: int) -> list[_Segment]:
    counts.parts += 1
    if depth > MAX_DEPTH or counts.parts > MAX_PARTS:
        raise ProviderResponseError("Gmail message structure is too large to read.")
    mime = _mime(part)
    if mime.startswith("multipart/"):
        children = part.get("parts", [])
        if not isinstance(children, list):
            raise ProviderResponseError("Gmail returned a malformed message structure.")
        segments: list[_Segment] = []
        for child in children:
            if not isinstance(child, dict):
                raise ProviderResponseError("Gmail returned a malformed message structure.")
            segments.extend(_collect(child, counts, depth + 1))
        if mime != "multipart/alternative" or not segments:
            return segments
        merged = _Segment()
        for segment in segments:
            merged.plain.extend(segment.plain)
            merged.html.extend(segment.html)
        return [merged]
    if mime in {"text/plain", "text/html"} and not _is_attachment(part):
        return [_Segment(plain=[part])] if mime == "text/plain" else [_Segment(html=[part])]
    if _is_attachment(part) or _has_content(part):
        counts.attachments += 1
    return []


async def _read(part: dict[str, object], counts: _Counts, fetch_part: PartFetcher) -> str:
    body = part.get("body")
    if not isinstance(body, dict):
        return ""
    data = body.get("data")
    attachment = body.get("attachmentId")
    if not data and isinstance(attachment, str) and attachment:
        size = body.get("size")
        if not isinstance(size, int) or size > MAX_SEPARATE_PART_BYTES:
            counts.unreadable += 1
            return ""
        try:
            fetched = await fetch_part(attachment)
        except ProviderResponseError:
            fetched = None
        data = fetched.get("data") if fetched else None
        if not isinstance(data, str) or not data:
            counts.unreadable += 1
            return ""
    if not isinstance(data, str) or not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        counts.unreadable += 1
        return ""
    text = _decode(raw, _charset(part))
    if len(text) > MAX_PART_CHARS:
        counts.truncated = True
        text = text[:MAX_PART_CHARS]
    return text


async def _read_all(
    parts: list[dict[str, object]], counts: _Counts, fetch_part: PartFetcher
) -> str:
    texts = [await _read(part, counts, fetch_part) for part in parts]
    return "\n\n".join(text.strip() for text in texts if text.strip())


async def _resolve(segment: _Segment, counts: _Counts, fetch_part: PartFetcher) -> tuple[str, bool]:
    plain = await _read_all(segment.plain, counts, fetch_part)
    if len(plain) >= STUB_PLAIN_CHARS or not segment.html:
        return plain, False
    html = html_to_text(await _read_all(segment.html, counts, fetch_part))
    if html and (not plain or len(html) > 3 * len(plain)):
        return html, True
    return plain, False


async def extract_body(
    message: dict[str, object], identifier: str, fetch_part: PartFetcher
) -> MessageBody:
    """Readable text of one message; file attachments are counted, never downloaded."""
    payload = message.get("payload")
    if message.get("id") != identifier or not isinstance(payload, dict):
        raise ProviderResponseError("Gmail returned a malformed message.")
    counts = _Counts()
    texts: list[str] = []
    used_html = False
    for segment in _collect(payload, counts, depth=0):
        text, from_html = await _resolve(segment, counts, fetch_part)
        if text:
            texts.append(text)
            used_html = used_html or from_html
    combined = "\n\n".join(texts).strip()
    truncated = counts.truncated or len(combined) > MAX_EXTRACTED_CHARS
    combined = combined[:MAX_EXTRACTED_CHARS].strip()
    source = BodySource.NONE
    if combined:
        source = BodySource.HTML if used_html else BodySource.PLAIN
    return MessageBody(
        provider_message_id=identifier,
        text=combined,
        source=source,
        attachments_skipped=counts.attachments,
        unreadable_parts=counts.unreadable,
        extraction_truncated=truncated,
    )
