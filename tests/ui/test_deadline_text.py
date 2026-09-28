"""A deadline in a few words: an exact time in the owner's zone, a date, or the phrase."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from mailbrief.domain.analysis import DeadlinePrecision
from mailbrief.ui.deadline_text import deadline_text
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
        (FRIDAY_NIGHT, TORONTO, "2026-10-03 02:00"),  # Saturday in Toronto.
        (FRIDAY_NIGHT, ZoneInfo("America/Los_Angeles"), "2026-10-02 23:00"),
        (BY_FRIDAY, TORONTO, "2026-10-02"),
        (BY_FRIDAY, ZoneInfo("Pacific/Kiritimati"), "2026-10-02"),
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
