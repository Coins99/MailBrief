"""Assemble deterministic daily briefs from analyzed messages and save them with coverage."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.analysis import AnalysisCategory, DeadlinePrecision, MessageAnalysis
from mailbrief.domain.briefs import AnalysisOutcome
from mailbrief.domain.digests import DailyDigest, DigestCoverage, DigestSection, DigestStatus
from mailbrief.domain.messages import RankedMessage
from mailbrief.services.analysis import PlannedMessage
from mailbrief.services.calendar import DayWindow
from mailbrief.storage.repositories import DigestRepository

SECTION_ORDER = (
    DigestSection.ACTIONS,
    DigestSection.DEADLINES,
    DigestSection.DECISIONS,
    DigestSection.HIGHLIGHTS,
)
_IN_BRIEF = (AnalysisOutcome.ANALYZED, AnalysisOutcome.REUSED)


def section_for(analysis: MessageAnalysis) -> DigestSection:
    """The one section an analyzed message belongs to."""
    if analysis.action_required:
        return DigestSection.ACTIONS
    if analysis.deadline_precision is not DeadlinePrecision.NONE:
        return DigestSection.DEADLINES
    if analysis.category is AnalysisCategory.DECISION:
        return DigestSection.DECISIONS
    return DigestSection.HIGHLIGHTS


def _due_at(analysis: MessageAnalysis) -> datetime | None:
    """When a dated deadline falls due: its instant, or the end of its local day."""
    if analysis.deadline_precision is DeadlinePrecision.DATETIME:
        return analysis.deadline_at_utc
    if (
        analysis.deadline_precision is DeadlinePrecision.DATE
        and analysis.deadline_date is not None
        and analysis.deadline_timezone is not None
    ):
        next_day = analysis.deadline_date + timedelta(days=1)
        zone = ZoneInfo(analysis.deadline_timezone)
        return datetime.combine(next_day, time.min, tzinfo=zone).astimezone(UTC)
    return None


@dataclass(frozen=True, slots=True)
class _Entry:
    ranked: RankedMessage
    analysis: MessageAnalysis
    message_row_id: int
    analysis_row_id: int


def _entry(item: PlannedMessage) -> _Entry:
    if item.analysis is None or item.message_row_id is None or item.analysis_row_id is None:
        raise ValueError("an analyzed or reused message needs its analysis and row IDs")
    return _Entry(item.ranked, item.analysis, item.message_row_id, item.analysis_row_id)


def _order(entry: _Entry) -> tuple[int, int, float, int, float, str]:
    """Section, then dated deadlines by due instant, then rank, recency and ID."""
    due = _due_at(entry.analysis)
    message = entry.ranked.message
    return (
        SECTION_ORDER.index(section_for(entry.analysis)),
        0 if due is not None else 1,
        due.timestamp() if due is not None else 0.0,
        -entry.ranked.score,
        -message.received_at_utc.timestamp(),
        message.provider_message_id,
    )


class DigestService:
    """Save one local day's brief from the analyzed shortlist."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._digests = DigestRepository(session)

    async def save(
        self,
        *,
        account_id: int,
        account_email: str,
        window: DayWindow,
        messages: Sequence[PlannedMessage],
        coverage: DigestCoverage,
    ) -> DailyDigest | None:
        """Save and return the day's brief.

        Returns None without writing when nothing was usable and something failed, so
        the last good brief for the day stays in place.
        """
        entries = sorted(
            (_entry(item) for item in messages if item.outcome in _IN_BRIEF), key=_order
        )
        if entries:
            complete = coverage.failed == 0 and coverage.sync_complete
            status = DigestStatus.COMPLETE if complete else DigestStatus.PARTIAL
        elif coverage.failed == 0:
            status = DigestStatus.EMPTY
        else:
            return None
        items = [
            (entry.message_row_id, entry.analysis_row_id, position, section_for(entry.analysis))
            for position, entry in enumerate(entries)
        ]
        saved = await self._digests.save_digest(
            account_id=account_id,
            local_date=window.local_date,
            timezone_name=window.timezone_name,
            status=status,
            items=items,
            coverage=coverage,
        )
        await self._session.commit()
        rows = await self._digests.get_digest_items(saved.id)
        return DigestRepository.to_domain(saved, rows, account_identity=account_email)
