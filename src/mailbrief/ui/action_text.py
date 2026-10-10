"""What the Actions page says about an action, worked out once for its list text, its painted
row and its detail pane, so they can't disagree."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from mailbrief.domain.actions import TARGET_REASON_TEXT, Action, ActionProposal, ActionStatus
from mailbrief.domain.analysis import ActionOwnership
from mailbrief.ui.deadline_text import day_text, deadline_text
from mailbrief.ui.proposals_view import pending_proposals

_WEEKDAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class ActionDetails:
    """An action's dates, progress, state and thread activity, as of ``now``.

    ``waiting``, ``overdue`` and ``carried_over`` hold only for an open action;
    ``target_reason`` is set only while the target is still the suggested one.
    """

    target: date | None
    target_reason: str | None
    due: str | None
    steps_done: int
    steps: int
    waiting: bool
    overdue: bool
    carried_over: bool
    source_gone: bool
    new_messages: int
    activity: str | None
    replied: str | None
    proposals: tuple[ActionProposal, ...]
    completed: date | None
    today: date  # The owner's day, which decides whether a date names its year.


def action_details(action: Action, *, today: date, zone: ZoneInfo, now: datetime) -> ActionDetails:
    """The details of ``action``; dates and times read in ``zone``, the owner's."""
    is_open = action.status is ActionStatus.OPEN
    reason = action.target_reason
    keeps_reason = (
        reason is not None
        and action.target_date is not None
        and action.target_date == action.suggested_target_date
    )
    activity = replied = None
    new_messages = 0
    thread = action.thread
    if thread is not None:
        new_messages = thread.new_messages
        if thread.latest_at_utc is not None and thread.latest_sender is not None:
            latest = _when(thread.latest_at_utc, today=today, zone=zone, clock=True)
            activity = (
                f"{thread.new_messages} new in thread, latest {latest} from {thread.latest_sender}"
            )
        if thread.owner_replied_at_utc is not None:
            day = _when(thread.owner_replied_at_utc, today=today, zone=zone, clock=False)
            replied = f"you replied {day}"
    completed = action.completed_at_utc
    return ActionDetails(
        target=action.target_date,
        target_reason=TARGET_REASON_TEXT[reason] if keeps_reason and reason is not None else None,
        due=deadline_text(action, zone, today),
        steps_done=sum(step.done for step in action.steps),
        steps=len(action.steps),
        waiting=is_open and action.ownership is ActionOwnership.WAITING_FOR,
        overdue=is_open and action.is_overdue(now),
        carried_over=is_open and action.carried_over(today, zone),
        source_gone=bool(action.sources) and not any(source.available for source in action.sources),
        new_messages=new_messages,
        activity=activity,
        replied=replied,
        proposals=pending_proposals(action),
        completed=None if completed is None else completed.astimezone(zone).date(),
        today=today,
    )


def target_text(details: ActionDetails, *, reason: bool = True) -> str | None:
    """The target as "Target Fri Oct 9"; with ``reason``, the reason too in the brief
    detail's wording while it applies: "Target Fri Oct 9, one working day before the
    deadline"."""
    target = details.target
    if target is None:
        return None
    text = f"Target {day_text(target, details.today)}"
    if not reason or details.target_reason is None:
        return text
    return f"{text}, {details.target_reason}"


def completed_text(details: ActionDetails) -> str | None:
    """The completion as "Completed Oct 6", the day in the owner's zone, or None for an open
    action."""
    day = details.completed
    return None if day is None else f"Completed {day:%b} {day.day}"


def gmail_source(action: Action) -> str | None:
    """The first source link that opens Gmail, if any; nothing else is ever opened."""
    for source in action.sources:
        link = source.web_link
        if link.scheme == "https" and link.host == "mail.google.com":
            return str(link)
    return None


def _when(moment: datetime, *, today: date, zone: ZoneInfo, clock: bool) -> str:
    """A moment of the past week by weekday ("Tue 14:02"), an older one by its date ("Sun
    Sep 20, 10:30", with the year when it isn't this year's), in ``zone``.

    Weekday names are fixed, like the rest of the window's English text.
    """
    local = moment.astimezone(zone)
    if today - timedelta(days=6) <= local.date() <= today:
        return _WEEKDAYS[local.weekday()] + (f" {local:%H:%M}" if clock else "")
    day = day_text(local.date(), today)
    return f"{day}, {local:%H:%M}" if clock else day
