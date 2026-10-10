"""Dates and deadlines in a few words, in one style for the whole window: the brief, its
suggestion and proposal lines, and the Actions page. The action editor's date fields stay
ISO, since dates are typed there."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from mailbrief.domain.actions import Action, ActionProposal
from mailbrief.domain.analysis import ActionSuggestion, DeadlinePrecision
from mailbrief.domain.digests import DigestItem


def day_text(day: date, today: date | None = None) -> str:
    """A day as "Mon Oct 5"; with ``today`` given and in another year, with the year too:
    "Mon Jan 4, 2027"."""
    text = f"{day:%a %b} {day.day}"
    return text if today is None or day.year == today.year else f"{text}, {day.year}"


def moment_text(moment: datetime, zone: ZoneInfo, today: date | None = None) -> str:
    """A moment as "Thu Oct 8, 17:00" in ``zone``, with the year as ``day_text`` adds it."""
    local = moment.astimezone(zone)
    return f"{day_text(local.date(), today)}, {local:%H:%M}"


def deadline_text(
    item: Action | ActionSuggestion | ActionProposal | DigestItem,
    zone: ZoneInfo,
    today: date | None = None,
) -> str | None:
    """How a deadline reads, or None when there is none.

    - exact: "Thu Oct 8, 17:00" in ``zone``, the owner's;
    - date only: "Mon Oct 5", the day the email names, whatever the owner's zone;
    - unresolved: the email's phrase in quotes.

    With ``today`` (the owner's day), a deadline in another year names it: "Mon Jan 4, 2027".
    """
    if item.deadline_precision is DeadlinePrecision.DATETIME and item.deadline_at_utc:
        return moment_text(item.deadline_at_utc, zone, today)
    if item.deadline_precision is DeadlinePrecision.DATE and item.deadline_date:
        return day_text(item.deadline_date, today)
    if item.deadline_precision is DeadlinePrecision.UNRESOLVED and item.deadline_text:
        return f"“{item.deadline_text}”"
    return None
