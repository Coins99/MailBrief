"""A deadline in a few words, for the brief's suggestion and proposal lines and the action
lists."""

from zoneinfo import ZoneInfo

from mailbrief.domain.actions import Action, ActionProposal
from mailbrief.domain.analysis import ActionSuggestion, DeadlinePrecision


def deadline_text(item: Action | ActionSuggestion | ActionProposal, zone: ZoneInfo) -> str | None:
    """How a deadline reads in a list, or None when there is none.

    - exact: "YYYY-MM-DD HH:MM" in ``zone``, the owner's, like the editor and the CLI;
    - date only: "YYYY-MM-DD", the day the email names, whatever the owner's zone;
    - unresolved: the email's phrase in quotes.
    """
    if item.deadline_precision is DeadlinePrecision.DATETIME and item.deadline_at_utc:
        return f"{item.deadline_at_utc.astimezone(zone):%Y-%m-%d %H:%M}"
    if item.deadline_precision is DeadlinePrecision.DATE and item.deadline_date:
        return item.deadline_date.isoformat()
    if item.deadline_precision is DeadlinePrecision.UNRESOLVED and item.deadline_text:
        return f"“{item.deadline_text}”"
    return None
