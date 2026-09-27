"""Resolve provider-reported deadlines against the email, in Python and without guessing."""

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from mailbrief.domain.analysis import (
    DEADLINE_TEXT_MAX_CHARS,
    AnalysisCandidate,
    AnalysisRequest,
    DeadlinePrecision,
)
from mailbrief.text.matching import appears_in

_PAST_DAYS = 31
_FUTURE_DAYS = 366
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME_PATTERN = re.compile(r"([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?")
_ZONE_KEY_PATTERN = re.compile(r"[A-Za-z]+(/[A-Za-z0-9_+-]+)+")
_UTC_NAMES = {"utc": "UTC", "etc/utc": "Etc/UTC"}
# Words in a deadline phrase that name a time zone. When the model reports no zone for such a
# phrase, the owner's zone would be a guess, so only the day is kept. Upper-case
# abbreviations ending in T (ET, PT, PST, EDT, CEST, AEST) are matched case-sensitively so
# "at 5pm" never matches; a false positive such as "SUBMIT IT BY 5PM" only drops the time.
ZONE_TOKEN_PATTERN = re.compile(
    r"\b[A-Z]{1,4}T\b"
    r"|(?i:\b(?:UTC|GMT)(?:\s?[+-]\d{1,2}(?::?\d{2})?)?\b)"
    r"|(?i:\b(?:Eastern|Pacific|Central|Mountain|Atlantic)\b)"
    r"|北京时间|东八区"
)


class InvalidDeadlineError(ValueError):
    """The deadline has a date or time without a phrase, or its phrase is not in the email."""


@dataclass(frozen=True, slots=True)
class ResolvedDeadline:
    """Deadline fields ready for MessageAnalysis."""

    text: str | None
    precision: DeadlinePrecision
    date: dt.date | None
    at_utc: dt.datetime | None
    timezone: str | None


_NO_DEADLINE = ResolvedDeadline(None, DeadlinePrecision.NONE, None, None, None)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _load_zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        return None


def _usable_stated_zone(value: str, request: AnalysisRequest) -> str | None:
    """A stated zone that the email itself writes and that names UTC or one IANA region.

    A zone the email does not contain is the model's guess, so it is never used.
    Abbreviations ("EST", "PT"), offsets ("UTC+2") and Etc/ zones are never resolved:
    people write "EST" year-round to mean Eastern Time, which tzdata models as fixed UTC-5.
    """
    if not appears_in(value, request.subject, request.body_text):
        return None
    utc = _UTC_NAMES.get(value.casefold())
    if utc is not None:
        return utc
    if _ZONE_KEY_PATTERN.fullmatch(value) is None or value.casefold().startswith("etc/"):
        return None
    return value if _load_zone(value) is not None else None


def _zone_for_time(phrase: str, stated_zone: str | None, request: AnalysisRequest) -> str | None:
    """The zone to place a deadline time in, or None to keep only the day.

    1. A stated zone that the name rules accept and the email writes is used.
    2. Otherwise a stated zone equal to the owner's zone, which the request supplies, is a
       model echo and counts as no stated zone.
    3. Any other stated zone keeps only the day.
    With no stated zone, the owner's zone is used unless the phrase names another zone.
    """
    if stated_zone is not None:
        usable = _usable_stated_zone(stated_zone, request)
        if usable is not None:
            return usable
        if stated_zone.casefold() != request.timezone_name.strip().casefold():
            return None  # Not written in the email, or a zone we will not guess.
    if ZONE_TOKEN_PATTERN.search(phrase):
        return None  # The phrase names a zone the model did not report.
    return request.timezone_name


def _parse_date(value: str, received: dt.date) -> dt.date | None:
    """A real YYYY-MM-DD day in the plausible window around the received day, else None."""
    if _DATE_PATTERN.fullmatch(value) is None:
        return None
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        return None
    earliest = received - dt.timedelta(days=_PAST_DAYS)
    latest = received + dt.timedelta(days=_FUTURE_DAYS)
    return day if earliest <= day <= latest else None


def _parse_time(value: str) -> dt.time | None:
    """An H:MM or HH:MM[:SS] time within the day, without seconds, else None."""
    match = _TIME_PATTERN.fullmatch(value)
    if match is None:
        return None
    hour, minute = int(match[1]), int(match[2])
    second = 0 if match[3] is None else int(match[3])
    if hour > 23 or minute > 59 or second > 59:
        return None
    return dt.time(hour, minute)


def resolve_deadline(candidate: AnalysisCandidate, request: AnalysisRequest) -> ResolvedDeadline:
    """Check the candidate's deadline against its email and resolve it to a day or instant.

    Raises InvalidDeadlineError, whose messages are static, when a date or time comes
    without a phrase or the phrase is not in the email. An unusable date keeps the phrase
    as unresolved, and an unusable time keeps only the date. The zone for a time is
    chosen by _zone_for_time; when there is none, only the day is kept. Offsets are never
    guessed.
    """
    text = _clean(candidate.deadline_text)
    raw_date = _clean(candidate.deadline_date)
    raw_time = _clean(candidate.deadline_time)
    stated_zone = _clean(candidate.stated_timezone)

    if text is None:
        if raw_date is not None or raw_time is not None:
            raise InvalidDeadlineError("a deadline date or time requires deadline text")
        return _NO_DEADLINE
    if len(text) > DEADLINE_TEXT_MAX_CHARS or not appears_in(
        text, request.subject, request.body_text
    ):
        raise InvalidDeadlineError("deadline text must quote the email")
    unresolved = ResolvedDeadline(text, DeadlinePrecision.UNRESOLVED, None, None, None)
    if raw_date is None:
        return unresolved  # A time without a day cannot be placed.
    received = request.received_at_utc.astimezone(ZoneInfo(request.timezone_name)).date()
    due_date = _parse_date(raw_date, received)
    if due_date is None:
        return unresolved
    date_only = ResolvedDeadline(
        text, DeadlinePrecision.DATE, due_date, None, request.timezone_name
    )
    due_time = None if raw_time is None else _parse_time(raw_time)
    if due_time is None:
        return date_only
    zone_name = _zone_for_time(text, stated_zone, request)
    if zone_name is None:
        return date_only
    zone = ZoneInfo(zone_name)
    at_utc = dt.datetime.combine(due_date, due_time, tzinfo=zone).astimezone(dt.UTC)
    # Re-derive the day so a time inside a DST gap stays consistent with its instant.
    local_date = at_utc.astimezone(zone).date()
    return ResolvedDeadline(text, DeadlinePrecision.DATETIME, local_date, at_utc, zone_name)
