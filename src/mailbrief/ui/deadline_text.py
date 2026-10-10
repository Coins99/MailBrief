"""Dates and deadlines in a few words, in one style for the whole window: the brief, its
suggestion and proposal lines, and the Actions page. The action editor's date fields stay
ISO, since dates are typed there."""

from datetime import date, datetime
from typing import Final
from zoneinfo import ZoneInfo

from mailbrief.domain.actions import Action, ActionProposal
from mailbrief.domain.analysis import ActionSuggestion, DeadlinePrecision
from mailbrief.domain.digests import DigestItem

# English, whatever the system language: Qt applies it to C date formatting, so strftime's
# weekday and month directives would follow it. Every weekday and month name in the window
# comes from these tables.
WEEKDAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def weekday(day: date) -> str:
    """A day's weekday as "Mon"."""
    return WEEKDAYS[day.weekday()]


def month_day(day: date, *, today: date) -> str:
    """A day as "Oct 5"; with the year too when it isn't ``today``'s: "Jan 4, 2027"."""
    text = f"{MONTHS[day.month - 1]} {day.day}"
    return text if day.year == today.year else f"{text}, {day.year}"


def day_text(day: date, *, today: date) -> str:
    """A day as "Mon Oct 5"; in a year other than ``today``'s (the owner's day), with the
    year too: "Mon Jan 4, 2027"."""
    return f"{weekday(day)} {month_day(day, today=today)}"


def moment_text(moment: datetime, zone: ZoneInfo, *, today: date) -> str:
    """A moment as "Thu Oct 8, 17:00" in ``zone``, with the year as ``day_text`` adds it."""
    local = moment.astimezone(zone)
    return f"{day_text(local.date(), today=today)}, {local:%H:%M}"


def deadline_text(
    item: Action | ActionSuggestion | ActionProposal | DigestItem,
    zone: ZoneInfo,
    *,
    today: date,
) -> str | None:
    """How a deadline reads, or None when there is none.

    - exact: "Thu Oct 8, 17:00" in ``zone``, the owner's;
    - date only: "Mon Oct 5", the day the email names, whatever the owner's zone;
    - unresolved: the email's phrase in quotes.

    A deadline in a year other than ``today``'s (the owner's day) names it: "Mon Jan 4, 2027".
    """
    if item.deadline_precision is DeadlinePrecision.DATETIME and item.deadline_at_utc:
        return moment_text(item.deadline_at_utc, zone, today=today)
    if item.deadline_precision is DeadlinePrecision.DATE and item.deadline_date:
        return day_text(item.deadline_date, today=today)
    if item.deadline_precision is DeadlinePrecision.UNRESOLVED and item.deadline_text:
        return f"“{item.deadline_text}”"
    return None
