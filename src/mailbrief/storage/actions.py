"""Stored suggestions, the owner's decisions on them, and accepted actions with their plans.

repositories.py imports this module, so it must never import repositories.py.
"""

import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import HttpUrl
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import (
    Action,
    ActionFilter,
    ActionSource,
    ActionStatus,
    ActionStep,
    SuggestionState,
    SuggestionView,
)
from mailbrief.domain.analysis import (
    ActionEffort,
    ActionOwnership,
    ActionSuggestion,
    DeadlinePrecision,
    TargetReason,
)
from mailbrief.storage.database import MAX_SQLITE_BATCH_SIZE
from mailbrief.storage.tables import (
    AccountTable,
    ActionSourceTable,
    ActionStepTable,
    ActionSuggestionTable,
    ActionTable,
    AnalysisTable,
    MessageTable,
    SuggestionDecisionTable,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DecisionKey:
    """What a decision is kept under: the provider message and the suggestion's fingerprint.

    It names no internal row, so a decision outlives the message, cache and account rows
    and applies again when the same message is synced once more.
    """

    provider: str
    provider_account_id: str
    provider_message_id: str
    fingerprint: str


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


def _chunks[T: (int, str)](values: Iterable[T]) -> Iterator[list[T]]:
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
    is empty without an analysis or suggestions. A decision is found by its full key
    (provider, provider account ID, provider message ID and title fingerprint), so one
    from another account never matches:

    - none: pending;
    - dismissed: dismissed;
    - accepted, with its action live: accepted, with the action's public ID;
    - accepted, with its action soft-deleted: dismissed;
    - accepted, with its action gone: pending.

    A stored row that no longer validates is skipped, and only the count is logged. The
    rows are read with four queries (suggestions, message identities, decisions,
    actions), each chunked.
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

    # (provider, provider account ID, provider message ID) -> message ID. Accounts and
    # messages are unique on these, so each identity names one message.
    identities: dict[tuple[str, str, str], int] = {}
    with_suggestions = (message_id for message_id, analysis_id in wanted if analysis_id in rows)
    for chunk in _chunks(with_suggestions):
        result_identities = await session.execute(
            select(
                MessageTable.id,
                AccountTable.provider,
                AccountTable.provider_account_id,
                MessageTable.provider_message_id,
            )
            .join(AccountTable, MessageTable.account_id == AccountTable.id)
            .where(MessageTable.id.in_(chunk))
        )
        for message_id, provider, account_id, provider_message_id in result_identities.tuples():
            identities[(provider, account_id, provider_message_id)] = message_id

    decisions: dict[tuple[int, str], SuggestionDecisionTable] = {}
    providers = sorted({provider for provider, _, _ in identities})
    account_ids = sorted({account_id for _, account_id, _ in identities})
    for chunk_ids in _chunks(provider_message_id for _, _, provider_message_id in identities):
        # The account filters let SQLite use the identity index; the match below is exact.
        result_decisions = await session.scalars(
            select(SuggestionDecisionTable).where(
                SuggestionDecisionTable.provider.in_(providers),
                SuggestionDecisionTable.provider_account_id.in_(account_ids),
                SuggestionDecisionTable.provider_message_id.in_(chunk_ids),
            )
        )
        for decision in result_decisions:
            matched = identities.get(
                (decision.provider, decision.provider_account_id, decision.provider_message_id)
            )
            if matched is not None:
                decisions[(matched, decision.fingerprint)] = decision

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


def action_from_rows(
    row: ActionTable,
    steps: Sequence[ActionStepTable],
    sources: Sequence[tuple[ActionSourceTable, bool | None]],
) -> Action:
    """Rebuild an action; each source pairs its snapshot with the cached Inbox state, if any."""
    return Action(
        public_id=row.public_id,
        title=row.title,
        ownership=ActionOwnership(row.ownership),
        status=ActionStatus(row.status),
        effort=None if row.effort is None else ActionEffort(row.effort),
        deadline_text=row.deadline_text,
        deadline_precision=DeadlinePrecision(row.deadline_precision),
        deadline_date=row.deadline_date,
        deadline_at_utc=row.deadline_at_utc,
        deadline_timezone=row.deadline_timezone,
        suggested_target_date=row.suggested_target_date,
        target_reason=None if row.target_reason is None else TargetReason(row.target_reason),
        target_date=row.target_date,
        notes=row.notes,
        evidence=row.evidence,
        created_at_utc=row.created_at_utc,
        updated_at_utc=row.updated_at_utc,
        completed_at_utc=row.completed_at_utc,
        revision=row.revision,
        steps=tuple(
            ActionStep(
                step_id=step.id,
                position=step.position,
                text=step.text,
                done=step.done,
                done_at_utc=step.done_at_utc,
            )
            for step in steps
        ),
        sources=tuple(
            ActionSource(
                provider_message_id=source.provider_message_id,
                subject=source.subject,
                sender_address=source.sender_address,
                web_link=HttpUrl(source.web_link),
                received_at_utc=source.received_at_utc,
                available=source.message_id is not None,
                in_inbox=in_inbox,
            )
            for source, in_inbox in sources
        ),
    )


class ActionRepository:
    """Accepted actions, their steps and sources, and the owner's decisions on suggestions.

    Decisions and actions are changed through ORM objects, never bulk statements, so rows
    already loaded in the session always show what was last written.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_suggestion(
        self, suggestion_id: int
    ) -> tuple[ActionSuggestionTable, MessageTable, AccountTable] | None:
        """A stored suggestion with the message its analysis describes and its account."""
        result = await self._session.execute(
            select(ActionSuggestionTable, MessageTable, AccountTable)
            .join(AnalysisTable, ActionSuggestionTable.analysis_id == AnalysisTable.id)
            .join(MessageTable, AnalysisTable.message_id == MessageTable.id)
            .join(AccountTable, MessageTable.account_id == AccountTable.id)
            .where(ActionSuggestionTable.id == suggestion_id)
            .execution_options(populate_existing=True)
        )
        found = result.tuples().first()
        return None if found is None else (found[0], found[1], found[2])

    async def get_decision(self, key: DecisionKey) -> SuggestionDecisionTable | None:
        result = await self._session.scalars(
            select(SuggestionDecisionTable).where(
                SuggestionDecisionTable.provider == key.provider,
                SuggestionDecisionTable.provider_account_id == key.provider_account_id,
                SuggestionDecisionTable.provider_message_id == key.provider_message_id,
                SuggestionDecisionTable.fingerprint == key.fingerprint,
            )
        )
        return result.first()

    async def save_decision(
        self,
        key: DecisionKey,
        *,
        decision: SuggestionState,
        action_id: int | None,
        decided_at_utc: datetime,
    ) -> None:
        """Record a decision, updating the existing row for this key."""
        if decision is SuggestionState.PENDING:
            raise ValueError("pending is the absence of a decision")
        row = await self.get_decision(key)
        if row is None:
            self._session.add(
                SuggestionDecisionTable(
                    provider=key.provider,
                    provider_account_id=key.provider_account_id,
                    provider_message_id=key.provider_message_id,
                    fingerprint=key.fingerprint,
                    decision=decision.value,
                    action_id=action_id,
                    decided_at_utc=decided_at_utc,
                )
            )
            return
        row.decision = decision.value
        row.action_id = action_id
        row.decided_at_utc = decided_at_utc

    async def delete_decision(self, key: DecisionKey) -> None:
        row = await self.get_decision(key)
        if row is not None:
            await self._session.delete(row)

    async def delete_decisions_for(self, action_id: int) -> None:
        rows = await self._session.scalars(
            select(SuggestionDecisionTable).where(SuggestionDecisionTable.action_id == action_id)
        )
        for row in rows.all():
            await self._session.delete(row)

    async def add_action(self, row: ActionTable) -> ActionTable:
        """Add a new action and flush it, so it has an ID."""
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_action(
        self, public_id: str, *, include_deleted: bool = False
    ) -> ActionTable | None:
        stmt = select(ActionTable).where(ActionTable.public_id == public_id)
        if not include_deleted:
            stmt = stmt.where(ActionTable.deleted_at_utc.is_(None))
        return (await self._session.scalars(stmt)).first()

    async def get_action_by_id(self, action_id: int) -> ActionTable | None:
        """The action with this row ID, even when it is soft-deleted."""
        return await self._session.get(ActionTable, action_id)

    async def delete_action(self, row: ActionTable) -> None:
        """Delete an action outright; its steps and sources go with it."""
        await self._session.delete(row)

    async def step_rows(self, action_id: int) -> list[ActionStepTable]:
        result = await self._session.scalars(
            select(ActionStepTable)
            .where(ActionStepTable.action_id == action_id)
            .order_by(ActionStepTable.position, ActionStepTable.id)
        )
        return list(result.all())

    def add_step(
        self, action_id: int, position: int, text: str, done_at_utc: datetime | None
    ) -> None:
        """Add a step; it is done exactly when it has a completion time."""
        self._session.add(
            ActionStepTable(
                action_id=action_id,
                position=position,
                text=text,
                done=done_at_utc is not None,
                done_at_utc=done_at_utc,
            )
        )

    async def delete_step(self, row: ActionStepTable) -> None:
        await self._session.delete(row)

    async def add_source(
        self, action_id: int, message: MessageTable, account: AccountTable
    ) -> bool:
        """Link a message snapshot, with its provider, account and thread so the thread can
        be tracked (ADR 0015); False when the action already has this message."""
        existing = await self._session.scalar(
            select(ActionSourceTable.id).where(
                ActionSourceTable.action_id == action_id,
                ActionSourceTable.provider_message_id == message.provider_message_id,
            )
        )
        if existing is not None:
            return False
        self._session.add(
            ActionSourceTable(
                action_id=action_id,
                message_id=message.id,
                provider_message_id=message.provider_message_id,
                subject=message.subject,
                sender_address=message.sender_address,
                web_link=message.web_link,
                received_at_utc=message.received_at_utc,
                provider=account.provider,
                provider_account_id=account.provider_account_id,
                provider_thread_id=message.conversation_id,
            )
        )
        return True

    async def source_count(self, action_id: int) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(ActionSourceTable)
            .where(ActionSourceTable.action_id == action_id)
        )
        return count or 0

    @staticmethod
    def _in_view(view: ActionFilter) -> list[ColumnElement[bool]]:
        """The conditions for a live action to appear in ``view``."""
        live = ActionTable.deleted_at_utc.is_(None)
        if view is ActionFilter.COMPLETED:
            return [live, ActionTable.status == ActionStatus.COMPLETED.value]
        ownership = (
            ActionOwnership.MINE if view is ActionFilter.OPEN else ActionOwnership.WAITING_FOR
        )
        return [
            live,
            ActionTable.status == ActionStatus.OPEN.value,
            ActionTable.ownership == ownership.value,
        ]

    async def list_rows(self, view: ActionFilter, *, limit: int | None) -> list[ActionTable]:
        """Live actions for a view: completed ones newest first, others in ID order.

        ``limit`` caps only the completed view here; the service sorts and caps the others.
        """
        stmt = select(ActionTable).where(*self._in_view(view))
        if view is ActionFilter.COMPLETED:
            stmt = stmt.order_by(ActionTable.completed_at_utc.desc(), ActionTable.id.desc())
            if limit is not None:
                stmt = stmt.limit(limit)
        else:
            stmt = stmt.order_by(ActionTable.id)
        return list((await self._session.scalars(stmt)).all())

    async def count_rows(self, view: ActionFilter) -> int:
        count = await self._session.scalar(
            select(func.count()).select_from(ActionTable).where(*self._in_view(view))
        )
        return count or 0

    async def load(self, rows: Sequence[ActionTable]) -> list[Action]:
        """Domain actions for rows, reading steps and sources with one chunked query each."""
        ids = [row.id for row in rows]
        steps: dict[int, list[ActionStepTable]] = {}
        for chunk in _chunks(ids):
            step_result = await self._session.scalars(
                select(ActionStepTable)
                .where(ActionStepTable.action_id.in_(chunk))
                .order_by(ActionStepTable.action_id, ActionStepTable.position, ActionStepTable.id)
            )
            for step in step_result:
                steps.setdefault(step.action_id, []).append(step)
        sources: dict[int, list[tuple[ActionSourceTable, bool | None]]] = {}
        for chunk in _chunks(ids):
            source_result = await self._session.execute(
                select(ActionSourceTable, MessageTable.is_in_inbox)
                .outerjoin(MessageTable, ActionSourceTable.message_id == MessageTable.id)
                .where(ActionSourceTable.action_id.in_(chunk))
                .order_by(ActionSourceTable.action_id, ActionSourceTable.id)
            )
            for source, in_inbox in source_result.tuples():
                sources.setdefault(source.action_id, []).append((source, in_inbox))
        return [
            action_from_rows(row, steps.get(row.id, ()), sources.get(row.id, ())) for row in rows
        ]
