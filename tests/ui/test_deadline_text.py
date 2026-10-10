"""Dates and deadlines in the window's one style: an exact time in the owner's zone, a
date, or the phrase, with the year only when it isn't the owner's."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from mailbrief.domain.analysis import DeadlinePrecision
from mailbrief.ui.deadline_text import day_text, deadline_text, moment_text
from tests.factories import make_action, make_suggestion

TORONTO = ZoneInfo("America/Toronto")
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
    assert deadline_text(make_action(**deadline), zone) == shown
    assert deadline_text(make_suggestion(**deadline), zone) == shown


TODAY = date(2026, 10, 1)


def test_a_date_reads_like_the_brief() -> None:
    assert deadline_text(make_action(**BY_FRIDAY), TORONTO, TODAY) == "Fri Oct 2"


def test_a_time_reads_like_the_brief_in_the_owner_s_zone() -> None:
    # 06:00 UTC on Saturday is still Friday evening in Los Angeles.
    assert deadline_text(make_action(**FRIDAY_NIGHT), TORONTO, TODAY) == "Sat Oct 3, 02:00"
    assert (
        deadline_text(make_action(**FRIDAY_NIGHT), ZoneInfo("America/Los_Angeles"), TODAY)
        == "Fri Oct 2, 23:00"
    )


def test_an_unresolved_deadline_is_the_email_s_own_phrase() -> None:
    assert deadline_text(make_action(**SOON), TORONTO, TODAY) == "“soon”"


def test_a_date_in_another_year_names_it() -> None:
    next_year = {**BY_FRIDAY, "deadline_date": date(2027, 1, 4)}
    new_year_eve = {
        **FRIDAY_NIGHT,
        "deadline_date": date(2026, 12, 31),
        "deadline_at_utc": datetime(2027, 1, 1, 2, 30, tzinfo=UTC),  # Still 2026 in Toronto.
    }

    assert deadline_text(make_action(**next_year), TORONTO, TODAY) == "Mon Jan 4, 2027"
    assert deadline_text(make_action(**next_year), TORONTO) == "Mon Jan 4"  # No day to compare.
    assert deadline_text(make_action(**new_year_eve), TORONTO, TODAY) == "Thu Dec 31, 21:30"
    assert (
        deadline_text(make_action(**new_year_eve), TORONTO, date(2027, 1, 2))
        == "Thu Dec 31, 2026, 21:30"
    )


def test_day_and_moment_text() -> None:
    assert day_text(date(2026, 10, 5)) == "Mon Oct 5"
    assert day_text(date(2025, 12, 30), date(2026, 1, 2)) == "Tue Dec 30, 2025"
    assert moment_text(datetime(2026, 10, 8, 21, tzinfo=UTC), TORONTO) == "Thu Oct 8, 17:00"
