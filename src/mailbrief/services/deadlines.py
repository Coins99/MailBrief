"""Resolve provider-reported deadlines against the email, in Python and without guessing."""

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from mailbrief.domain.analysis import AnalysisCandidate, AnalysisRequest, DeadlinePrecision
from mailbrief.text.matching import appears_in

MAX_DEADLINE_TEXT_CHARS = 500
_PAST_DAYS = 31
_FUTURE_DAYS = 366
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME_PATTERN = re.compile(r"([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?")


class InvalidDeadlineError(ValueError):
    """The deadline is inconsistent, not quoted from the email, or out of range."""


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


def _parse_date(value: str) -> dt.date:
    if _DATE_PATTERN.fullmatch(value) is None:
        raise InvalidDeadlineError("deadline date must be YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise InvalidDeadlineError("deadline date must be a real calendar day") from None


def _parse_time(value: str) -> dt.time:
    match = _TIME_PATTERN.fullmatch(value)
    if match is None:
        raise InvalidDeadlineError("deadline time must be HH:MM or HH:MM:SS")
    hour, minute = int(match[1]), int(match[2])
    second = 0 if match[3] is None else int(match[3])
    if hour > 23 or minute > 59 or second > 59:
        raise InvalidDeadlineError("deadline time must be between 00:00 and 23:59")
    return dt.time(hour, minute)


def resolve_deadline(candidate: AnalysisCandidate, request: AnalysisRequest) -> ResolvedDeadline:
    """Check the candidate's deadline against its email and resolve it to a day or instant.

    Raises InvalidDeadlineError, whose messages are static, when the fields disagree, the
    phrase is not in the email or the date is implausible. Offsets are never guessed.
    """
    text = _clean(candidate.deadline_text)
    raw_date = _clean(candidate.deadline_date)
    raw_time = _clean(candidate.deadline_time)
    stated_zone = _clean(candidate.stated_timezone)

    if text is None:
        if raw_date is not None or raw_time is not None:
            raise InvalidDeadlineError("a deadline date or time requires deadline text")
        return _NO_DEADLINE
    if len(text) > MAX_DEADLINE_TEXT_CHARS or not appears_in(
        text, request.subject, request.body_text
    ):
        raise InvalidDeadlineError("deadline text must quote the email")
    if raw_date is None:
        if raw_time is not None:
            raise InvalidDeadlineError("a deadline time requires a date")
        return ResolvedDeadline(text, DeadlinePrecision.UNRESOLVED, None, None, None)

    due_date = _parse_date(raw_date)
    received = request.received_at_utc.astimezone(ZoneInfo(request.timezone_name)).date()
    earliest = received - dt.timedelta(days=_PAST_DAYS)
    latest = received + dt.timedelta(days=_FUTURE_DAYS)
    if not earliest <= due_date <= latest:
        raise InvalidDeadlineError("deadline date is outside the plausible window")
    if raw_time is None:
        return ResolvedDeadline(text, DeadlinePrecision.DATE, due_date, None, request.timezone_name)

    due_time = _parse_time(raw_time)
    zone_name = request.timezone_name if stated_zone is None else stated_zone
    zone = _load_zone(zone_name)
    if zone is None:
        # An abbreviation such as "PT" names no single offset: keep the day, never guess.
        return ResolvedDeadline(text, DeadlinePrecision.DATE, due_date, None, request.timezone_name)
    at_utc = dt.datetime.combine(due_date, due_time, tzinfo=zone).astimezone(dt.UTC)
    # Re-derive the day so a time inside a DST gap stays consistent with its instant.
    local_date = at_utc.astimezone(zone).date()
    return ResolvedDeadline(text, DeadlinePrecision.DATETIME, local_date, at_utc, zone_name)
