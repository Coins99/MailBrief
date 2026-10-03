"""Unit tests for calendar boundary calculations and timezone handling."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mailbrief.services.calendar import (
    DayWindow,
    day_window,
    graph_date_filter,
    local_day_window,
    resolve_timezone,
)


@pytest.mark.parametrize(
    ("zone_name", "test_dt", "expected_hours"),
    [
        # Spring forward with midnight gap (00:00 -> 01:00)
        ("America/Santiago", datetime(2022, 9, 11, 12, 0, tzinfo=UTC), 23),
        # Fall back with ambiguous midnight (01:00 -> 00:00)
        ("America/Havana", datetime(2022, 11, 6, 12, 0, tzinfo=UTC), 25),
        # Standard US 2:00 AM spring forward
        ("America/Los_Angeles", datetime(2025, 3, 9, 15, 0, tzinfo=UTC), 23),
        # Standard US 2:00 AM fall back
        ("America/Los_Angeles", datetime(2025, 11, 2, 15, 0, tzinfo=UTC), 25),
        # Normal 24h day across various timezones
        ("America/New_York", datetime(2026, 6, 15, 12, 0, tzinfo=UTC), 24),
        ("Asia/Tokyo", datetime(2026, 6, 15, 12, 0, tzinfo=UTC), 24),
        ("Australia/Adelaide", datetime(2026, 6, 15, 12, 0, tzinfo=UTC), 24),
        ("Asia/Kathmandu", datetime(2026, 6, 15, 12, 0, tzinfo=UTC), 24),
        ("Pacific/Auckland", datetime(2026, 12, 31, 23, 0, tzinfo=UTC), 24),
        ("UTC", datetime(2026, 8, 31, 14, 0, tzinfo=UTC), 24),
    ],
)
def test_local_day_window_invariants(
    zone_name: str, test_dt: datetime, expected_hours: int
) -> None:
    tz = ZoneInfo(zone_name)
    window = local_day_window(test_dt, tz)

    assert window.start_utc.tzinfo is UTC
    assert window.end_utc.tzinfo is UTC
    assert window.start_utc < window.end_utc

    duration = window.end_utc - window.start_utc
    assert duration == timedelta(hours=expected_hours)
    assert window.timezone_name in {zone_name, tz.key}


def test_local_day_window_tokyo_date_split() -> None:
    # 2026-06-15 16:00 UTC is 2026-06-16 01:00 in Tokyo (UTC+9)
    dt_utc = datetime(2026, 6, 15, 16, 0, tzinfo=UTC)
    tz = ZoneInfo("Asia/Tokyo")
    window = local_day_window(dt_utc, tz)

    assert window.local_date == date(2026, 6, 16)
    assert window.start_utc == datetime(2026, 6, 15, 15, 0, tzinfo=UTC)
    assert window.end_utc == datetime(2026, 6, 16, 15, 0, tzinfo=UTC)


def test_local_day_window_year_boundary() -> None:
    # Dec 31 in Auckland (UTC+13 during summer DST)
    dt_utc = datetime(2026, 12, 31, 10, 0, tzinfo=UTC)
    tz = ZoneInfo("Pacific/Auckland")
    window = local_day_window(dt_utc, tz)

    assert window.local_date == date(2026, 12, 31)
    assert window.start_utc == datetime(2026, 12, 30, 11, 0, tzinfo=UTC)
    assert window.end_utc == datetime(2026, 12, 31, 11, 0, tzinfo=UTC)


def test_graph_date_filter_formatting() -> None:
    window = DayWindow(
        local_date=date(2026, 8, 31),
        timezone_name="UTC",
        start_utc=datetime(2026, 8, 31, 0, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC),
    )
    query_filter = graph_date_filter(window)
    assert (
        query_filter
        == "receivedDateTime ge 2026-08-31T00:00:00Z and receivedDateTime lt 2026-09-01T00:00:00Z"
    )


def test_resolve_timezone_valid() -> None:
    tz = resolve_timezone("America/New_York")
    assert tz.key == "America/New_York"


def test_resolve_timezone_invalid() -> None:
    with pytest.raises(ValueError, match="Unknown or invalid timezone"):
        resolve_timezone("Invalid/NonExistentZone")


def test_resolve_timezone_default_none() -> None:
    tz = resolve_timezone(None)
    assert isinstance(tz, ZoneInfo)


def test_resolve_timezone_fallback_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_localzone() -> None:
        raise RuntimeError("tzlocal failed")

    monkeypatch.setattr("tzlocal.get_localzone", fake_get_localzone)
    tz = resolve_timezone(None)
    assert tz.key == "UTC"


@pytest.mark.parametrize(
    ("zone_name", "day", "hours"),
    [
        ("America/Toronto", date(2026, 9, 29), 24),
        ("Asia/Tokyo", date(2026, 1, 1), 24),
        ("America/Toronto", date(2026, 3, 8), 23),  # 02:00 spring forward
        ("America/Toronto", date(2026, 11, 1), 25),  # 02:00 fall back
        ("America/Havana", date(2026, 3, 8), 23),  # Midnight doesn't exist: 00:00 -> 01:00.
        ("America/Havana", date(2026, 11, 1), 25),  # 01:00 -> 00:00, so midnight repeats.
        ("America/Santiago", date(2026, 9, 6), 23),  # 00:00 -> 01:00
        ("UTC", date(2026, 2, 28), 24),
    ],
)
def test_day_window_matches_the_window_of_any_moment_that_day(
    zone_name: str, day: date, hours: int
) -> None:
    tz = ZoneInfo(zone_name)
    window = day_window(day, tz)

    assert window.local_date == day
    assert window.timezone_name == zone_name
    assert window.end_utc - window.start_utc == timedelta(hours=hours)
    # The first instant, a midday instant and the last instant of the day all agree.
    for moment in (
        window.start_utc,
        window.start_utc + timedelta(hours=hours / 2),
        window.end_utc - timedelta(microseconds=1),
    ):
        assert local_day_window(moment, tz) == window
    assert local_day_window(window.end_utc, tz).local_date == day + timedelta(days=1)
