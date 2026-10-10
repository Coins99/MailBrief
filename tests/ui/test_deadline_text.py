"""Dates and deadlines in the window's one style: an exact time in the owner's zone, a
date, or the phrase, with the year only when it isn't the owner's."""

import locale
from collections.abc import Iterator
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from mailbrief.domain.analysis import DeadlinePrecision
from mailbrief.domain.digests import DigestItem
from mailbrief.ui.brief_list import deadline_chip
from mailbrief.ui.deadline_text import (
    day_text,
    deadline_text,
    moment_text,
    month_day,
    weekday,
)
from tests.factories import make_action, make_digest_item, make_suggestion

TORONTO = ZoneInfo("America/Toronto")
TODAY = date(2026, 10, 1)
FRIDAY_NIGHT = {
    "deadline_text": "Friday 11 PM Pacific",
    "deadline_precision": DeadlinePrecision.DATETIME,
    "deadline_date": date(2026, 10, 2),
    "deadline_at_utc": datetime(2026, 10, 3, 6, 0, tzinfo=UTC),
    "deadline_timezone": "America/Los_Angeles",
}
BY_FRIDAY = {
    "deadline_text": "by Friday",
    "deadline_precision": DeadlinePrecision.DATE,
    "deadline_date": date(2026, 10, 2),
    "deadline_timezone": "America/Los_Angeles",
}
SOON = {"deadline_text": "soon", "deadline_precision": DeadlinePrecision.UNRESOLVED}


@pytest.mark.parametrize(
    ("deadline", "zone", "shown"),
    [
        (FRIDAY_NIGHT, TORONTO, "Sat Oct 3, 02:00"),  # Saturday in Toronto.
        (FRIDAY_NIGHT, ZoneInfo("America/Los_Angeles"), "Fri Oct 2, 23:00"),
        (BY_FRIDAY, TORONTO, "Fri Oct 2"),
        (BY_FRIDAY, ZoneInfo("Pacific/Kiritimati"), "Fri Oct 2"),
        (SOON, TORONTO, "“soon”"),
        ({}, TORONTO, None),
    ],
    ids=["exact-toronto", "exact-own-zone", "date", "date-far-east", "unresolved", "none"],
)
def test_actions_and_suggestions_read_the_same(
    deadline: dict[str, object], zone: ZoneInfo, shown: str | None
) -> None:
    assert deadline_text(make_action(**deadline), zone, today=TODAY) == shown
    assert deadline_text(make_suggestion(**deadline), zone, today=TODAY) == shown


def test_a_date_reads_like_the_brief() -> None:
    assert deadline_text(make_action(**BY_FRIDAY), TORONTO, today=TODAY) == "Fri Oct 2"


def test_a_time_reads_like_the_brief_in_the_owner_s_zone() -> None:
    # 06:00 UTC on Saturday is still Friday evening in Los Angeles.
    assert deadline_text(make_action(**FRIDAY_NIGHT), TORONTO, today=TODAY) == "Sat Oct 3, 02:00"
    assert (
        deadline_text(make_action(**FRIDAY_NIGHT), ZoneInfo("America/Los_Angeles"), today=TODAY)
        == "Fri Oct 2, 23:00"
    )


def test_an_unresolved_deadline_is_the_email_s_own_phrase() -> None:
    assert deadline_text(make_action(**SOON), TORONTO, today=TODAY) == "“soon”"


def test_a_date_in_another_year_names_it() -> None:
    next_year = {**BY_FRIDAY, "deadline_date": date(2027, 1, 4)}
    new_year_eve = {
        **FRIDAY_NIGHT,
        "deadline_date": date(2026, 12, 31),
        "deadline_at_utc": datetime(2027, 1, 1, 2, 30, tzinfo=UTC),  # Still 2026 in Toronto.
    }

    assert deadline_text(make_action(**next_year), TORONTO, today=TODAY) == "Mon Jan 4, 2027"
    assert deadline_text(make_action(**new_year_eve), TORONTO, today=TODAY) == "Thu Dec 31, 21:30"
    assert (
        deadline_text(make_action(**new_year_eve), TORONTO, today=date(2027, 1, 2))
        == "Thu Dec 31, 2026, 21:30"
    )


def test_day_and_moment_text() -> None:
    assert day_text(date(2026, 10, 5), today=TODAY) == "Mon Oct 5"
    assert day_text(date(2025, 12, 30), today=date(2026, 1, 2)) == "Tue Dec 30, 2025"
    moment = datetime(2026, 10, 8, 21, tzinfo=UTC)
    assert moment_text(moment, TORONTO, today=TODAY) == "Thu Oct 8, 17:00"
    assert month_day(date(2026, 10, 5), today=TODAY) == "Oct 5"
    assert month_day(date(2027, 10, 5), today=TODAY) == "Oct 5, 2027"
    assert weekday(date(2026, 10, 5)) == "Mon"


def chip_text(item: DigestItem) -> str | None:
    chip = deadline_chip(item, TORONTO, TODAY, TORONTO)
    return None if chip is None else chip.text


@pytest.fixture
def french_dates() -> Iterator[None]:
    """C date formatting in French, when this system has a French locale; else skip."""
    saved = locale.setlocale(locale.LC_TIME)
    for name in ("fr_FR.UTF-8", "fr_FR.utf8", "fr_FR", "French_France.1252", "French"):
        try:
            locale.setlocale(locale.LC_TIME, name)
            break
        except locale.Error:
            continue
    else:
        pytest.skip("no French locale on this system")
    try:
        yield
    finally:
        locale.setlocale(locale.LC_TIME, saved)


def test_dates_stay_english_whatever_the_system_language(french_dates: None) -> None:
    monday = date(2026, 10, 5)
    assert monday.strftime("%a") != "Mon"  # The locale really is in effect.
    assert day_text(monday, today=TODAY) == "Mon Oct 5"
    assert day_text(date(2026, 8, 3), today=TODAY) == "Mon Aug 3"  # "août" in French.
    assert day_text(date(2026, 12, 2), today=TODAY) == "Wed Dec 2"  # "déc." in French.
    assert month_day(date(2026, 2, 9), today=TODAY) == "Feb 9"  # "févr." in French.
    assert weekday(date(2026, 10, 7)) == "Wed"  # "mer." in French.
    assert moment_text(datetime(2026, 8, 3, 21, tzinfo=UTC), TORONTO, today=TODAY) == (
        "Mon Aug 3, 17:00"
    )
    item = make_digest_item(
        deadline_precision=DeadlinePrecision.DATETIME,
        deadline_at_utc=datetime(2026, 8, 21, 21, tzinfo=UTC),
    )
    assert chip_text(item) == "Due Aug 21 17:00"
    soon = make_digest_item(
        deadline_precision=DeadlinePrecision.DATETIME,
        deadline_at_utc=datetime(2026, 10, 2, 21, tzinfo=UTC),
    )
    assert chip_text(soon) == "Due Fri 17:00"
    dated = make_digest_item(
        deadline_precision=DeadlinePrecision.DATE,
        deadline_date=date(2026, 2, 9),
        deadline_at_utc=None,
    )
    assert chip_text(dated) == "Due Feb 9"
