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
_INVALID_RULE: Final = "A sender rule must be an address or @domain."
_INVALID_ZONE: Final = "The time zone must be UTC or a region such as America/Toronto."


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


def normalize_exclusion(text: str) -> str:
    """A sender rule in its stored form: an exact address, or ``@domain``.

    Text without "@" is a domain. Rules are case-folded. Raises ValueError with a static
    message that never echoes the text.
    """
    rule = text.strip().casefold()
    if "@" not in rule:
        rule = "@" + rule
    if len(rule) > EXCLUSION_MAX_CHARS or any(
        character.isspace() or unicodedata.category(character).startswith("C") for character in rule
    ):
        raise ValueError(_INVALID_RULE)
    local, _, domain = rule.partition("@")
    if "@" in domain or not domain or (not local and "." not in domain):
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
