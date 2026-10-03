"""Shared domain-model behavior."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Immutable base for values crossing application boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def counted(count: int, noun: str) -> str:
    """``count`` with its noun, plural unless the count is 1: "1 message", "3 messages".
    For regular nouns only; a sentence with an irregular plural or a verb that agrees
    spells it out itself."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def utc_now() -> datetime:
    """The current time, aware and in UTC: the default clock everywhere a clock is injected."""
    return datetime.now(UTC)


def normalize_utc(value: datetime) -> datetime:
    """Require an aware timestamp and normalize it to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a time zone")
    return value.astimezone(UTC)
