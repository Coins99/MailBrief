"""Calendar boundary calculation and timezone utilities for MailBrief."""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tzlocal

from mailbrief.domain.common import normalize_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DayWindow:
    """Represents a local calendar day converted to UTC boundaries."""

    local_date: date
    timezone_name: str
    start_utc: datetime
    end_utc: datetime


def resolve_timezone(tz_key: str | None = None) -> ZoneInfo:
    """Resolve an IANA timezone key or discover the system local timezone.

    Raises ValueError if an explicit tz_key is invalid or not found.
    Falls back to UTC if system timezone discovery fails.
    """
    if tz_key and tz_key.strip():
        cleaned_key = tz_key.strip()
        try:
            return ZoneInfo(cleaned_key)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError(f"Unknown or invalid timezone: '{tz_key}'") from exc

    try:
        return tzlocal.get_localzone()
    except Exception as exc:
        logger.warning("Failed to discover system local timezone (%s); falling back to UTC", exc)
        return ZoneInfo("UTC")


def _local_midnight_utc(d: date, tz: ZoneInfo) -> datetime:
    """Convert local midnight (00:00:00) on a given date to an aware UTC datetime.

    Why fold=0 is correct in all three timezone transition cases:
    1. Normal day: Unambiguous offset, fold has no effect.
    2. Ambiguous midnight (e.g. fall-back transition at 01:00 -> 00:00): 00:00 occurs
       twice. fold=0 selects the pre-transition offset (the earlier instant), which is
       when the local calendar day genuinely begins.
    3. Non-existent midnight (e.g. spring-forward transition at 00:00 -> 01:00): 00:00
       never occurs locally. fold=0 computes using the pre-transition offset, landing
       exactly on the transition instant (the first real instant of that local day).
    """
    naive = datetime.combine(d, time.min)
    return naive.replace(tzinfo=tz, fold=0).astimezone(UTC)


def local_day_window(now: datetime, tz: ZoneInfo) -> DayWindow:
    """Compute the UTC boundaries for the local calendar day containing `now`."""
    aware_now = normalize_utc(now) if now.tzinfo is None else now
    local_now = aware_now.astimezone(tz)
    local_d = local_now.date()

    start_utc = _local_midnight_utc(local_d, tz)
    end_utc = _local_midnight_utc(local_d + timedelta(days=1), tz)

    tz_name = getattr(tz, "key", None) or str(tz)
    return DayWindow(
        local_date=local_d,
        timezone_name=tz_name,
        start_utc=start_utc,
        end_utc=end_utc,
    )


def graph_date_filter(window: DayWindow) -> str:
    """Format Microsoft Graph $filter query expression with UTC 'Z' timestamps."""
    start_iso = window.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = window.end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"receivedDateTime ge {start_iso} and receivedDateTime lt {end_iso}"
