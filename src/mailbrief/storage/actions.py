"""Map stored action suggestions, and the owner's decisions on them, to domain models.

repositories.py imports this module, so it must never import repositories.py.
"""

import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import SuggestionState, SuggestionView
from mailbrief.domain.analysis import (
    ActionEffort,
    ActionOwnership,
    ActionSuggestion,
    DeadlinePrecision,
    TargetReason,
)
from mailbrief.storage.database import MAX_SQLITE_BATCH_SIZE
from mailbrief.storage.tables import ActionSuggestionTable, ActionTable, SuggestionDecisionTable

logger = logging.getLogger(__name__)


def suggestion_from_row(row: ActionSuggestionTable) -> ActionSuggestion:
    """Rebuild a stored suggestion; raises ValueError when the row no longer validates."""
    if not isinstance(row.steps_json, list):
        raise ValueError("stored suggestion steps must be a list")
    return ActionSuggestion(
        position=row.position,
        title=row.title,
        ownership=ActionOwnership(row.ownership),
        effort=None if row.effort is None else ActionEffort(row.effort),
        deadline_text=row.deadline_text,
        deadline_precision=DeadlinePrecision(row.deadline_precision),
        deadline_date=row.deadline_date,
        deadline_at_utc=row.deadline_at_utc,
        deadline_timezone=row.deadline_timezone,
        suggested_target_date=row.suggested_target_date,
        target_reason=None if row.target_reason is None else TargetReason(row.target_reason),
        steps=tuple(row.steps_json),
        evidence=row.evidence,
        fingerprint=row.fingerprint,
    )


def _chunks(values: Iterable[int]) -> Iterator[list[int]]:
    ordered = sorted(set(values))
    for start in range(0, len(ordered), MAX_SQLITE_BATCH_SIZE):
        yield ordered[start : start + MAX_SQLITE_BATCH_SIZE]


def _state(
    decision: SuggestionDecisionTable | None,
    actions: Mapping[int, tuple[str, datetime | None]],
) -> tuple[SuggestionState, str | None]:
    """The owner's decision on a suggestion and, when accepted, its live action's public ID."""
    if decision is None:
        return SuggestionState.PENDING, None
    if decision.decision == SuggestionState.DISMISSED.value:
        return SuggestionState.DISMISSED, None
    # Only accepted decisions are left; ``actions`` holds only their actions.
    action = None if decision.action_id is None else actions.get(decision.action_id)
    if action is None:
        return SuggestionState.PENDING, None  # The accepted action was deleted outright.
    public_id, deleted_at_utc = action
    if deleted_at_utc is not None:
        return SuggestionState.DISMISSED, None  # Deleting an accepted action dismisses it.
    return SuggestionState.ACCEPTED, public_id


async def suggestion_views(
    session: AsyncSession, pairs: Iterable[tuple[int, int | None]]
) -> dict[int, tuple[SuggestionView, ...]]:
    """Each message's suggestions, in position order, with the owner's decision on each.

    ``pairs`` holds (message ID, analysis ID or None). Every message gets an entry, which
    is empty without an analysis or suggestions. A decision is found by message and title
    fingerprint:

    - none: pending;
    - dismissed: dismissed;
    - accepted, with its action live: accepted, with the action's public ID;
    - accepted, with its action soft-deleted: dismissed;
    - accepted, with its action gone: pending.

    A stored row that no longer validates is skipped, and only the count is logged. The
    rows are read with three queries (suggestions, decisions, actions), each chunked.
    """
    wanted: list[tuple[int, int | None]] = list(pairs)
    rows: dict[int, list[ActionSuggestionTable]] = {}
    for chunk in _chunks(analysis_id for _, analysis_id in wanted if analysis_id is not None):
        result = await session.scalars(
            select(ActionSuggestionTable)
            .where(ActionSuggestionTable.analysis_id.in_(chunk))
            .order_by(ActionSuggestionTable.analysis_id, ActionSuggestionTable.position)
        )
        for row in result:
            rows.setdefault(row.analysis_id, []).append(row)

    decisions: dict[tuple[int, str], SuggestionDecisionTable] = {}
    with_suggestions = (message_id for message_id, analysis_id in wanted if analysis_id in rows)
    for chunk in _chunks(with_suggestions):
        result_decisions = await session.scalars(
            select(SuggestionDecisionTable).where(SuggestionDecisionTable.message_id.in_(chunk))
        )
        for decision in result_decisions:
            decisions[(decision.message_id, decision.fingerprint)] = decision

    actions: dict[int, tuple[str, datetime | None]] = {}
    accepted = (
        decision.action_id
        for decision in decisions.values()
        if decision.decision == SuggestionState.ACCEPTED.value and decision.action_id is not None
    )
    for chunk in _chunks(accepted):
        result_actions = await session.execute(
            select(ActionTable.id, ActionTable.public_id, ActionTable.deleted_at_utc).where(
                ActionTable.id.in_(chunk)
            )
        )
        for action_id, public_id, deleted_at_utc in result_actions.tuples():
            actions[action_id] = (public_id, deleted_at_utc)

    skipped = 0
    views: dict[int, tuple[SuggestionView, ...]] = {}
    for message_id, analysis_id in wanted:
        found: list[SuggestionView] = []
        stored: Sequence[ActionSuggestionTable] = (
            () if analysis_id is None else rows.get(analysis_id, ())
        )
        for row in stored:
            state, action_public_id = _state(decisions.get((message_id, row.fingerprint)), actions)
            try:
                found.append(
                    SuggestionView(
                        suggestion_id=row.id,
                        suggestion=suggestion_from_row(row),
                        state=state,
                        action_public_id=action_public_id,
                    )
                )
            except ValueError:
                skipped += 1
        views[message_id] = tuple(found)
    if skipped:
        logger.warning("Skipped %d stored suggestions that no longer validate", skipped)
    return views
