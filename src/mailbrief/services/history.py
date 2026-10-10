"""Saved briefs by day, and bounded catch-up for missed days (M8 Part 3).

A brief covers one local day in the owner's zone. Briefing a past day is explicit, one day
per run, at most CATCH_UP_DAYS back, and its coverage line says what it covers: a past day
only covers the messages still in the Inbox when it is made. Nothing here briefs a past day
by itself.
"""

from datetime import date, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.digests import DailyDigest, DigestSection, SavedBriefSummary
from mailbrief.storage.repositories import DigestRepository

CATCH_UP_DAYS: Final = 7
HISTORY_LIMIT: Final = 60
_OUT_OF_RANGE: Final = "Choose today or one of the previous 7 days."
_UNREADABLE: Final = "Write the date as YYYY-MM-DD, such as 2026-09-29."


class BriefDateError(ValueError):
    """A date that can't be briefed; the message is static."""

    def __init__(self, message: str = _OUT_OF_RANGE) -> None:
        super().__init__(message)


def parse_brief_date(text: str) -> date:
    """A YYYY-MM-DD date, or BriefDateError."""
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise BriefDateError(_UNREADABLE) from None


def check_brief_date(day: date, today: date) -> None:
    """Raise BriefDateError unless ``day`` is today or one of the CATCH_UP_DAYS before it."""
    if day > today or day < today - timedelta(days=CATCH_UP_DAYS):
        raise BriefDateError()


def catch_up_days(today: date) -> tuple[date, ...]:
    """The CATCH_UP_DAYS days before today, newest first."""
    return tuple(today - timedelta(days=back) for back in range(1, CATCH_UP_DAYS + 1))


def coverage_line(brief: DailyDigest | SavedBriefSummary) -> str:
    """What a brief covers, in its own stored zone; shared by the CLI and the desktop.

    A brief made on its own day covers that day's messages up to when it was made. One made
    later covers only the messages still in the Inbox then: earlier ones may have left. A
    brief with replies from tracked threads that weren't in that Inbox says how many.
    """
    zone = brief.timezone_name
    made = brief.generated_at_utc.astimezone(ZoneInfo(zone))
    day = brief.local_date.isoformat()
    if made.date() <= brief.local_date:
        line = (
            f"Covers messages received on {day} up to {made:%H:%M} ({zone}) "
            "that were in your Inbox then."
        )
    else:
        line = (
            f"Covers messages received on {day} ({zone}) that were still in your Inbox on "
            f"{made.date().isoformat()} at {made:%H:%M}."
        )
    outside = outside_count(brief)
    if outside == 1:
        line += " Also includes 1 reply from a thread you track that wasn't in today's Inbox."
    elif outside:
        line += (
            f" Also includes {outside} replies from threads you track that weren't in "
            "today's Inbox."
        )
    return line


def outside_count(brief: DailyDigest | SavedBriefSummary) -> int:
    """How many replies from tracked threads that weren't in that day's Inbox it holds.
    The desktop's short coverage footer (``ui/workspace.py``) counts them too."""
    if isinstance(brief, SavedBriefSummary):
        return brief.follow_up_count
    return sum(item.section is DigestSection.FOLLOW_UPS for item in brief.items)


class BriefHistory:
    """Read saved Gmail briefs; nothing here contacts Gmail or an AI provider."""

    def __init__(self, session: AsyncSession) -> None:
        self._digests = DigestRepository(session)

    async def list_saved(self, limit: int = HISTORY_LIMIT) -> tuple[SavedBriefSummary, ...]:
        """Saved briefs of Gmail accounts, newest local date first."""
        return await self._digests.list_summaries(limit)

    async def get(self, account_email: str, local_date: date) -> DailyDigest | None:
        """One account's brief for a date, with its suggestions, like the latest one."""
        return await self._digests.get_for_account_date(account_email, local_date)

    async def accounts_for(self, local_date: date) -> tuple[str, ...]:
        """The Gmail accounts with a saved brief for a date, sorted."""
        return await self._digests.accounts_with_brief(local_date)

    async def missed_days(self, account_email: str, today: date) -> tuple[date, ...]:
        """Catch-up days, newest first, with no saved brief for the account.

        Any saved brief counts, even an empty or partial one.
        """
        days = catch_up_days(today)
        saved = await self._digests.saved_dates(account_email, days[-1], days[0])
        return tuple(day for day in days if day not in saved)
