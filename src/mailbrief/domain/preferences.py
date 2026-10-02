"""The owner's preferences, shared by the desktop and the CLI (ADR 0014).

Nothing here is secret. Sender rules are the owner's own text: they may be shown back to
the owner, but only their count is ever logged. Validation errors never echo the input.
"""

import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime
from typing import Final, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict, Field, field_validator

from mailbrief.domain.analysis import MAX_ANALYSIS_BATCH
from mailbrief.domain.bodies import MAX_ANALYSIS_CHARS
from mailbrief.domain.common import DomainModel, normalize_utc
from mailbrief.domain.drafts import DraftLength, DraftTone

SHORTLIST_LIMIT_MAX: Final = 10
# How often, in minutes, an open MailBrief may refresh on its own (ADR 0017); None is never.
REFRESH_INTERVALS: Final = (60, 120, 240)
EXCLUSIONS_MAX: Final = 200
EXCLUSION_MAX_CHARS: Final = 320
TIME_ZONE_MAX_CHARS: Final = 64
AI_BODY_CHARS_MAX: Final = MAX_ANALYSIS_CHARS
AI_OUTPUT_TOKENS_MIN: Final = 256
AI_OUTPUT_TOKENS_MAX: Final = 64_000
AI_REQUESTS_MAX: Final = 1_000
AI_TIMEOUT_MIN: Final = 10
AI_TIMEOUT_MAX: Final = 600
# The AI limits a preference can set; each is also a field of config.Settings.
AI_LIMIT_FIELDS: Final = (
    "ai_batch_size",
    "ai_body_character_limit",
    "ai_max_output_tokens",
    "ai_max_requests_per_run",
    "ai_timeout_seconds",
)

_REGION_ZONE: Final = re.compile(r"[A-Za-z]+(/[A-Za-z0-9_+-]+)+")
# Characters no local part of a sender rule may hold: they come from pasted headers and lists.
_LOCAL_FORBIDDEN: Final = frozenset('<>()[],;:"\\')
_INVALID_RULE: Final = "A sender rule must be an address or @domain."
_INVALID_ZONE: Final = "The time zone must be UTC or a region such as America/Toronto."
_INVALID_INTERVAL: Final = "Refresh every 60, 120 or 240 minutes, or never."


def is_region_zone(name: str) -> bool:
    """UTC, or an IANA region zone such as America/Toronto that loads here.

    Abbreviations (EST) and the fixed-offset Etc/ zones are refused: their names mislead,
    and Etc/GMT+5 is five hours behind UTC.
    """
    if name == "UTC":
        return True
    if (
        len(name) > TIME_ZONE_MAX_CHARS
        or not _REGION_ZONE.fullmatch(name)
        or name.casefold().startswith("etc/")
    ):
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _is_domain(domain: str) -> bool:
    """Two or more dot-separated labels of letters, digits and hyphens, none of them empty."""
    labels = domain.split(".")
    return len(labels) >= 2 and all(
        label and all(character.isalnum() or character == "-" for character in label)
        for label in labels
    )


def normalize_exclusion(text: str) -> str:
    """A sender rule in its stored form: an exact address, or ``@domain``.

    Text without "@" is a domain, and the wildcard forms ``*@domain`` and ``*.domain`` mean
    ``@domain``, which already covers the domain and its subdomains. Rules are case-folded.

    Every rule accepted can match a sender. An address has exactly one "@", a domain as
    _is_domain describes, and a local part with no whitespace, no control or format
    characters and none of ``_LOCAL_FORBIDDEN``, so a pasted ``<address>``, a ``mailto:``
    link, quotes or a trailing comma are refused rather than saved as a rule that never
    matches. Raises ValueError with a static message that never echoes the text.
    """
    rule = text.strip().casefold()
    if rule.startswith("*@"):
        rule = rule[1:]
    elif "@" not in rule:
        rule = "@" + rule.removeprefix("*.")
    if len(rule) > EXCLUSION_MAX_CHARS:
        raise ValueError(_INVALID_RULE)
    local, _, domain = rule.partition("@")
    if not _is_domain(domain) or any(
        character.isspace()
        or unicodedata.category(character).startswith("C")
        or character in _LOCAL_FORBIDDEN
        for character in local
    ):
        raise ValueError(_INVALID_RULE)
    return rule


def sender_excluded(address: str, rules: Iterable[str]) -> bool:
    """Whether a sender matches a rule: the exact address, or a ``@domain`` rule for its
    domain or any subdomain. ``@example.com`` does not match ``notexample.com``."""
    folded = address.strip().casefold()
    domain = folded.rpartition("@")[2] if "@" in folded else ""
    for rule in rules:
        if rule.startswith("@"):
            if domain and (domain == rule[1:] or domain.endswith("." + rule[1:])):
                return True
        elif folded == rule:
            return True
    return False


class PreferencesEdit(DomainModel):
    """What the owner can change. None for an AI limit means "use the default"."""

    model_config = ConfigDict(hide_input_in_errors=True)

    time_zone: str | None = Field(default=None, max_length=TIME_ZONE_MAX_CHARS)
    shortlist_limit: int = Field(default=SHORTLIST_LIMIT_MAX, ge=1, le=SHORTLIST_LIMIT_MAX)
    excluded_senders: tuple[str, ...] = ()
    draft_tone: DraftTone = DraftTone.NEUTRAL
    draft_length: DraftLength = DraftLength.MEDIUM
    ai_batch_size: int | None = Field(default=None, ge=1, le=MAX_ANALYSIS_BATCH)
    ai_body_character_limit: int | None = Field(default=None, ge=1, le=AI_BODY_CHARS_MAX)
    ai_max_output_tokens: int | None = Field(
        default=None, ge=AI_OUTPUT_TOKENS_MIN, le=AI_OUTPUT_TOKENS_MAX
    )
    ai_max_requests_per_run: int | None = Field(default=None, ge=1, le=AI_REQUESTS_MAX)
    ai_timeout_seconds: float | None = Field(default=None, ge=AI_TIMEOUT_MIN, le=AI_TIMEOUT_MAX)
    # Automatic refresh while the desktop is open (ADR 0017): when it starts, and how often.
    refresh_on_launch: bool = False
    refresh_interval_minutes: int | None = None

    @field_validator("refresh_interval_minutes")
    @classmethod
    def _interval(cls, value: int | None) -> int | None:
        if value is not None and value not in REFRESH_INTERVALS:
            raise ValueError(_INVALID_INTERVAL)
        return value

    @field_validator("time_zone")
    @classmethod
    def _region_zone(cls, value: str | None) -> str | None:
        if value is not None and not is_region_zone(value):
            raise ValueError(_INVALID_ZONE)
        return value

    @field_validator("excluded_senders")
    @classmethod
    def _rules(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Normalized, duplicates dropped keeping the first, at most EXCLUSIONS_MAX."""
        rules = tuple(dict.fromkeys(normalize_exclusion(rule) for rule in value))
        if len(rules) > EXCLUSIONS_MAX:
            raise ValueError(f"At most {EXCLUSIONS_MAX} sender rules.")
        return rules


class OwnerPreferences(PreferencesEdit):
    """The saved preferences; revision 0 means they were never saved."""

    revision: int = Field(ge=0)
    updated_at_utc: datetime | None = None

    @field_validator("updated_at_utc")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc(value)

    @classmethod
    def defaults(cls) -> Self:
        """What a fresh database means: the system time zone, ten messages, no rules and
        the built-in AI limits."""
        return cls(revision=0)
