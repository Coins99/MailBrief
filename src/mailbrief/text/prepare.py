"""Prepare readable email text for analysis: tidy it, drop trailing quoted history, bound it."""

import html
import re
import unicodedata

from mailbrief.text.html_to_text import bound_numeric_refs

MAX_ATTRIBUTION_CHARS = 300
MIN_DUPLICATE_CHARS = 30

_FORWARD_SUBJECT = re.compile(r"^\s*(fwd?|wg|tr|rv|转发|轉寄)\s*[:：]", re.IGNORECASE)
_FORWARD_MARKER = re.compile(
    r"^\s*(-{2,}\s*forwarded message\s*-{2,}|begin forwarded message:)\s*$", re.IGNORECASE
)
_ORIGINAL_MESSAGE = re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.IGNORECASE)
_UNDERSCORES = re.compile(r"^\s*_{10,}\s*$")
_OUTLOOK_FROM = re.compile(r"^\s*from\s*:", re.IGNORECASE)
_OUTLOOK_SENT = re.compile(r"^\s*(sent|date)\s*:", re.IGNORECASE)
_OUTLOOK_SUBJECT = re.compile(r"^\s*subject\s*:", re.IGNORECASE)
# Invisible padding that email templates use but Unicode doesn't class as a format character.
_FILLERS = frozenset("\u034f\u115f\u1160\u2800\u3164\uffa0")
# Marks removed markup so that lines containing nothing else can be dropped.
_REMOVED = "\ue000"
# These patterns stop at the next "<" or "(", so a failed match never rescans the rest of an
# untrusted text; comments are removed by _without_comments for the same reason.
_ENDIF = re.compile(r"<!\[endif\]-->", re.IGNORECASE)
_OFFICE_TAG = re.compile(r"</?[vwo]:[a-z]+\b[^<>]*>", re.IGNORECASE)
_BREAK_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)
_WRAPPED_LINK = re.compile(
    r"\(\s*(?:https?|mailto|tel):[^\s()]*\s*\)"
    r"|\[\s*(?:https?|mailto|tel):[^\s\[\]]*\s*\]"
    r"|<(?:https?|mailto|tel):[^\s<>]*>",
    re.IGNORECASE,
)
_BARE_LINK = re.compile(r"\b(?:https?://|mailto:|tel:)[^\s<>()\[\]]+", re.IGNORECASE)
_LINK_ONLY = re.compile(r"^(?:\[link\][\s|,.;:()-]*)+$")
_SPACES = re.compile(r"[^\S\n]+")


def looks_like_forward(subject: str | None) -> bool:
    """True for subjects such as "Fwd:", "FW:", "WG:" or "转发:"."""
    return bool(subject and _FORWARD_SUBJECT.match(subject))


def _keep(ch: str) -> bool:
    if ch in "\n\t":
        return True
    return ch not in _FILLERS and unicodedata.category(ch) not in {"Cc", "Cf"}


def _without_comments(text: str) -> str:
    """Replace each "<!-- ... -->" with the removal mark in a single pass.

    A regex would rescan the rest of the text for every unclosed "<!--", which is quadratic.
    """
    pieces: list[str] = []
    position = 0
    while (start := text.find("<!--", position)) != -1:
        end = text.find("-->", start + 4)
        if end == -1:
            break
        pieces += [text[position:start], _REMOVED]
        position = end + 3
    pieces.append(text[position:])
    return "".join(pieces)


def normalize_text(text: str) -> str:
    """Clean extracted text for reading and analysis.

    Removes HTML comments (including Outlook-only blocks), stray Office and ``<br>`` markup and
    HTML entities that some senders leave in plain-text versions. Link addresses are removed
    because tracking links often identify the reader; a link inside a sentence becomes
    ``[link]``. Invisible padding, control characters, repeated long paragraphs and extra
    blank lines go too.
    """
    text = _without_comments(text.replace("\r\n", "\n").replace("\r", "\n"))
    for pattern in (_ENDIF, _OFFICE_TAG):
        text = pattern.sub(_REMOVED, text)
    text = html.unescape(bound_numeric_refs(_BREAK_TAG.sub("\n", text)))
    text = _BARE_LINK.sub("[link]", _WRAPPED_LINK.sub(_REMOVED, text))
    text = "".join(ch for ch in text if _keep(ch))
    lines: list[str] = []
    for raw in text.split("\n"):
        line = _SPACES.sub(" ", raw.replace(_REMOVED, " ")).strip()
        if (not line and _REMOVED in raw) or _LINK_ONLY.match(line):
            continue
        lines.append(line)
    paragraphs: list[str] = []
    seen: set[str] = set()
    for paragraph in re.split(r"\n{2,}", "\n".join(lines)):
        paragraph = paragraph.strip()
        if not paragraph or (len(paragraph) >= MIN_DUPLICATE_CHARS and paragraph in seen):
            continue
        seen.add(paragraph)
        paragraphs.append(paragraph)
    return "\n\n".join(paragraphs)


def _is_quote(line: str) -> bool:
    return line.lstrip().startswith(">")


def _trailing_quote_start(lines: list[str]) -> int | None:
    """Where the final run of quoted (>) and blank lines begins, if it contains quotes."""
    index = len(lines)
    saw_quote = False
    while index > 0 and (not lines[index - 1].strip() or _is_quote(lines[index - 1])):
        index -= 1
        saw_quote = saw_quote or _is_quote(lines[index])
    return index if saw_quote else None


def _attribution_start(lines: list[str], quote_start: int) -> int:
    """Also drop the "On ... wrote:" line (in any language) directly above the quotes.

    Not in interleaved replies (quotes further up): there that line is part of an answer.
    """
    index = quote_start
    while index > 0 and not lines[index - 1].strip():
        index -= 1
    if index > 0 and not any(_is_quote(line) for line in lines[: index - 1]):
        header = lines[index - 1].strip()
        if header.endswith((":", "：")) and len(header) <= MAX_ATTRIBUTION_CHARS:
            return index - 1
    return quote_start


def _outlook_header_start(lines: list[str]) -> int | None:
    """First Outlook-style reply header.

    That is "-----Original Message-----", or From: with Sent:/Date: below it and either a line
    of underscores above or Subject: below. Other From:/Date: blocks, such as receipts or
    itineraries, are content.
    """
    for index, line in enumerate(lines):
        if _ORIGINAL_MESSAGE.match(line):
            return index
        below = lines[index + 1 : index + 6]
        if not _OUTLOOK_FROM.match(line) or not any(
            _OUTLOOK_SENT.match(following) for following in below[:3]
        ):
            continue
        if index > 0 and _UNDERSCORES.match(lines[index - 1]):
            return index - 1
        if any(_OUTLOOK_SUBJECT.match(following) for following in below):
            return index
    return None


def trim_quoted_history(text: str, *, is_forward: bool = False) -> tuple[str, bool]:
    """Remove quoted history that follows a reply; forwards are never trimmed.

    Returns the text and whether anything was removed. Nothing is removed when that would
    leave no text, so an email that is entirely quoted keeps its content.
    """
    lines = text.split("\n")
    if is_forward or any(_FORWARD_MARKER.match(line) for line in lines):
        return text, False
    cut: int | None = None
    quote_start = _trailing_quote_start(lines)
    if quote_start is not None:
        cut = _attribution_start(lines, quote_start)
    outlook = _outlook_header_start(lines)
    if outlook is not None and (cut is None or outlook < cut):
        cut = outlook
    if cut is None:
        return text, False
    kept = "\n".join(lines[:cut]).strip()
    if not kept:
        return text, False
    return kept, True


def truncate_at_boundary(text: str, limit: int) -> tuple[str, bool]:
    """At most ``limit`` characters, cut at a line or word break when one is near the end."""
    if len(text) <= limit:
        return text, False
    window = text[:limit]
    floor = int(limit * 0.8)
    for separator in ("\n", " "):
        position = window.rfind(separator)
        if position >= floor:
            return window[:position].rstrip(), True
    return window.rstrip(), True
