"""The owner's drafts and notes: kinds, limits, versions, placeholders and exports.

Drafts are the owner's own writing (ADR 0012). Their text is kept exactly as entered, so
these models turn off the whitespace stripping other domain models use. Nothing here reads
mail, and nothing here does I/O.
"""

import re
from datetime import datetime
from enum import StrEnum
from typing import Final, Self

from pydantic import ConfigDict, Field, HttpUrl, field_validator, model_validator

from mailbrief.domain.actions import ACTION_TITLE_MAX_CHARS, PUBLIC_ID_CHARS
from mailbrief.domain.common import DomainModel, normalize_utc

DRAFT_TITLE_MAX_CHARS: Final = 200
DRAFT_RECIPIENTS_MAX_CHARS: Final = 2_000
DRAFT_BODY_MAX_CHARS: Final = 20_000
MAX_DRAFT_VERSIONS: Final = 100
PREVIEW_MAX_CHARS: Final = 80
FILENAME_MAX_CHARS: Final = 80
FALLBACK_FILENAME: Final = "mailbrief-draft"

_PLACEHOLDER = re.compile(r"\[\[([^\[\]\n]{1,60})\]\]")
_RECIPIENT_SEPARATORS = re.compile(r"[,;]")
_FILENAME_SPACES = re.compile(r"\s+")
# Names Windows reserves for devices, with or without an extension.
_RESERVED_NAMES: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{digit}" for digit in range(1, 10)}
    | {f"LPT{digit}" for digit in range(1, 10)}
)


class DraftKind(StrEnum):
    """What a draft is for; only replies and emails have recipients."""

    REPLY = "reply"
    EMAIL = "email"
    NOTE = "note"
    MESSAGE = "message"


class DraftVersionOrigin(StrEnum):
    """Why a version was saved. GENERATED is reserved for AI drafting (M7 stage 2)."""

    CREATED = "created"
    EDITED = "edited"
    RESTORED = "restored"
    GENERATED = "generated"


EMAIL_KINDS: Final = frozenset({DraftKind.REPLY, DraftKind.EMAIL})
KIND_NAMES: Final[dict[DraftKind, str]] = {
    DraftKind.REPLY: "Reply",
    DraftKind.EMAIL: "Email",
    DraftKind.NOTE: "Note",
    DraftKind.MESSAGE: "Message",
}

# Drafts keep the owner's text exactly as typed, including leading and trailing spaces.
_OWNER_TEXT = ConfigDict(hide_input_in_errors=True, str_strip_whitespace=False)


def recipient_warnings(text: str) -> tuple[str, ...]:
    """The comma- or semicolon-separated items that are not one plausible address.

    An item is plausible when it has exactly one "@" with something on both sides, so
    "Alex <alex@example.com>" passes and "alex@" or "a@b@c" do not. Empty items are ignored.
    """
    warnings: list[str] = []
    for part in _RECIPIENT_SEPARATORS.split(text):
        item = part.strip()
        if not item:
            continue
        local, at, domain = item.partition("@")
        if not (at and local.strip() and domain.strip() and "@" not in domain):
            warnings.append(item)
    return tuple(warnings)


def placeholders(text: str) -> tuple[str, ...]:
    """The distinct ``[[...]]`` tokens in ``text``, in the order they first appear.

    A token holds 1 to 60 characters with no brackets or line breaks, so ``[[ ]]`` counts
    but ``[[]]``, ``[[a[b]]`` and a token split across lines do not.
    """
    return tuple(dict.fromkeys(match.group(0) for match in _PLACEHOLDER.finditer(text)))


def first_line(text: str, limit: int = PREVIEW_MAX_CHARS) -> str:
    """The first line with any text, stripped and cut to ``limit`` characters."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit].rstrip()
    return ""


class DraftSource(DomainModel):
    """A snapshot of an email a draft came from; ``available`` says whether it is cached."""

    model_config = ConfigDict(hide_input_in_errors=True)

    provider_message_id: str = Field(min_length=1, max_length=512)
    subject: str = Field(default="", max_length=998, repr=False)
    sender_address: str = Field(min_length=3, max_length=320, repr=False)
    web_link: HttpUrl
    received_at_utc: datetime
    available: bool

    @field_validator("received_at_utc")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        return normalize_utc(value)


class DraftEdit(DomainModel):
    """The owner's current text for a draft, saved as it is."""

    model_config = _OWNER_TEXT

    title: str = Field(default="", max_length=DRAFT_TITLE_MAX_CHARS, repr=False)
    to_text: str = Field(default="", max_length=DRAFT_RECIPIENTS_MAX_CHARS, repr=False)
    cc_text: str = Field(default="", max_length=DRAFT_RECIPIENTS_MAX_CHARS, repr=False)
    body: str = Field(default="", max_length=DRAFT_BODY_MAX_CHARS, repr=False)

    def has_recipients(self) -> bool:
        return bool(self.to_text.strip() or self.cc_text.strip())

    def content(self) -> "DraftEdit":
        """Just the text, as an edit that would save it unchanged."""
        return DraftEdit(
            title=self.title, to_text=self.to_text, cc_text=self.cc_text, body=self.body
        )


class Draft(DraftEdit):
    """A draft the owner is writing, with its links and the revision it was read at."""

    public_id: str = Field(min_length=PUBLIC_ID_CHARS, max_length=PUBLIC_ID_CHARS)
    kind: DraftKind
    action_public_id: str | None = Field(
        default=None, min_length=PUBLIC_ID_CHARS, max_length=PUBLIC_ID_CHARS
    )
    action_title: str | None = Field(
        default=None, min_length=1, max_length=ACTION_TITLE_MAX_CHARS, repr=False
    )
    sources: tuple[DraftSource, ...] = ()
    created_at_utc: datetime
    updated_at_utc: datetime
    revision: int = Field(ge=1)

    @field_validator("created_at_utc", "updated_at_utc")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        return normalize_utc(value)

    @model_validator(mode="after")
    def validate_recipients(self) -> Self:
        if self.kind not in EMAIL_KINDS and self.has_recipients():
            raise ValueError("only replies and emails can have recipients")
        return self

    @property
    def placeholders(self) -> tuple[str, ...]:
        """Placeholders still in the recipients, title or body, in that order."""
        return placeholders("\n".join((self.to_text, self.cc_text, self.title, self.body)))

    @property
    def display_title(self) -> str:
        """The title, else the body's first line (up to 80 characters), else "Untitled …"."""
        return self.title.strip() or first_line(self.body) or f"Untitled {self.kind.value}"


class DraftSummary(DomainModel):
    """One row of the drafts list; building it never reads versions."""

    model_config = ConfigDict(hide_input_in_errors=True)

    public_id: str = Field(min_length=PUBLIC_ID_CHARS, max_length=PUBLIC_ID_CHARS)
    kind: DraftKind
    display_title: str = Field(min_length=1, max_length=DRAFT_TITLE_MAX_CHARS, repr=False)
    updated_at_utc: datetime
    placeholder_count: int = Field(ge=0)
    action_title: str | None = Field(default=None, repr=False)
    source_subject: str | None = Field(default=None, repr=False)
    revision: int = Field(ge=1)  # So deleting from the list names the revision it showed.

    @field_validator("updated_at_utc")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        return normalize_utc(value)


class DraftVersionInfo(DomainModel):
    """One saved version as the versions list shows it."""

    model_config = ConfigDict(hide_input_in_errors=True)

    number: int = Field(ge=1)
    origin: DraftVersionOrigin
    created_at_utc: datetime
    preview: str = Field(max_length=PREVIEW_MAX_CHARS, repr=False)
    length: int = Field(ge=0)

    @field_validator("created_at_utc")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return normalize_utc(value)


class DraftVersion(DraftEdit):
    """One saved version's full text."""

    number: int = Field(ge=1)
    origin: DraftVersionOrigin
    created_at_utc: datetime

    @field_validator("created_at_utc")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return normalize_utc(value)


def _with_newline(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


def export_text(draft: Draft) -> str:
    """Plain text: To/Cc/Subject headers then the body for emails; else title then body.

    Cc appears only when set. The result always ends with one line break unless empty.
    """
    if draft.kind in EMAIL_KINDS:
        headers = [f"To: {draft.to_text.strip()}".rstrip()]
        if draft.cc_text.strip():
            headers.append(f"Cc: {draft.cc_text.strip()}")
        headers.append(f"Subject: {draft.title.strip()}".rstrip())
        return "\n".join(headers) + "\n\n" + _with_newline(draft.body)
    title = draft.title.strip()
    return (f"{title}\n\n" if title else "") + _with_newline(draft.body)


def export_markdown(draft: Draft) -> str:
    """Markdown: bold To/Cc/Subject lines then the body for emails; else a heading then body.

    The body is the owner's own text, so it is written as it is, Markdown and all.
    """
    if draft.kind in EMAIL_KINDS:
        headers = [f"**To:** {draft.to_text.strip()}".rstrip()]
        if draft.cc_text.strip():
            headers.append(f"**Cc:** {draft.cc_text.strip()}")
        headers.append(f"**Subject:** {draft.title.strip()}".rstrip())
        # Two trailing spaces make each header its own line.
        return "  \n".join(headers) + "\n\n" + _with_newline(draft.body)
    title = draft.title.strip()
    return (f"# {title}\n\n" if title else "") + _with_newline(draft.body)


def export_filename(draft: Draft, suffix: str) -> str:
    """A safe file name from the display title, such as ``Re Budget.md``.

    Only letters, digits, spaces and "-_." are kept, runs of spaces become one, and the
    name is at most 80 characters before ``suffix``. A name with nothing left, one made of
    dots, or one Windows reserves falls back to "mailbrief-draft".
    """
    kept = "".join(ch for ch in draft.display_title if ch.isalnum() or ch in " -_.")
    stem = _FILENAME_SPACES.sub(" ", kept).strip(" .")[:FILENAME_MAX_CHARS].rstrip(" .")
    if not stem or stem.split(".")[0].upper() in _RESERVED_NAMES:
        stem = FALLBACK_FILENAME
    return stem + suffix
