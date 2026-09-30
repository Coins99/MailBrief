"""Accept, dismiss and manage the owner's actions; every change is one committed revision.

Nothing here reads or sends email: suggestions come from stored analyses. Errors carry
static messages, and nothing logs action text.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import (
    MAX_ACTION_STEPS,
    Action,
    ActionEdit,
    ActionFilter,
    ActionStatus,
    StepEdit,
    SuggestionState,
)
from mailbrief.domain.analysis import ActionSuggestion
from mailbrief.domain.common import normalize_utc
from mailbrief.storage.actions import ActionRepository, DecisionKey, suggestion_from_row
from mailbrief.storage.tables import (
    ActionStepTable,
    ActionTable,
    MessageTable,
    SuggestionDecisionTable,
)

_NOT_FOUND: Final = "That action was not found."
_SUGGESTION_NOT_FOUND: Final = "That suggestion was not found."
_STALE: Final = "The action changed since it was loaded; reload it and try again."
_LATEST: Final = datetime.max.replace(tzinfo=UTC)
COMPLETED_LIST_LIMIT: Final = 200  # The newest completed actions a list shows by default.


class ActionNotFoundError(LookupError):
    """No live action has that public ID."""


class SuggestionNotFoundError(LookupError):
    """No stored suggestion has that ID, or its row no longer validates."""


class ActionConflictError(ValueError):
    """The action changed since it was read, or the change is not allowed now."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_public_id() -> str:
    return str(uuid.uuid4())


def _touch(row: ActionTable, now: datetime) -> None:
    row.revision += 1
    row.updated_at_utc = now


def _mark(step: ActionStepTable, done: bool, now: datetime) -> None:
    """A step that becomes done is timed now; it keeps its time while it stays done."""
    if not done:
        step.done, step.done_at_utc = False, None
    elif not step.done:
        step.done, step.done_at_utc = True, now


def _check_step_list(steps: Sequence[StepEdit]) -> list[int]:
    """The listed step IDs; raises ActionConflictError for too many steps or a repeated ID."""
    if len(steps) > MAX_ACTION_STEPS:
        raise ActionConflictError("An action has at most 30 steps.")
    listed = [edit.step_id for edit in steps if edit.step_id is not None]
    if len(listed) != len(set(listed)):
        raise ActionConflictError("Each step may appear only once.")
    return listed


def _apply_edit(row: ActionTable, edit: ActionEdit) -> None:
    row.title = edit.title
    row.ownership = edit.ownership.value
    row.effort = None if edit.effort is None else edit.effort.value
    row.target_date = edit.target_date
    row.notes = edit.notes


def _accepted_action_id(decision: SuggestionDecisionTable | None) -> int | None:
    if decision is None or decision.decision != SuggestionState.ACCEPTED.value:
        return None
    return decision.action_id


class ActionService:
    """The owner's accepted actions, and decisions on the suggestions they come from.

    Every mutating method reads the clock once, commits once and rolls back on failure.
    A method taking ``expected_revision`` raises ActionConflictError when the action's
    revision differs, so a stale screen never overwrites a newer change.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _new_public_id,
    ) -> None:
        self._session = session
        self._clock = clock
        self._id_factory = id_factory
        self._repository = ActionRepository(session)

    async def _write[T](self, operation: Callable[[datetime], Awaitable[T]]) -> T:
        now = normalize_utc(self._clock())
        try:
            result = await operation(now)
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return result

    async def _suggestion(
        self, suggestion_id: int
    ) -> tuple[ActionSuggestion, MessageTable, DecisionKey]:
        """The suggestion, its message and the key its decision is kept under."""
        found = await self._repository.get_suggestion(suggestion_id)
        if found is None:
            raise SuggestionNotFoundError(_SUGGESTION_NOT_FOUND)
        row, message, account = found
        try:
            suggestion = suggestion_from_row(row)
        except ValueError:
            raise SuggestionNotFoundError(_SUGGESTION_NOT_FOUND) from None
        key = DecisionKey(
            provider=account.provider,
            provider_account_id=account.provider_account_id,
            provider_message_id=message.provider_message_id,
            fingerprint=suggestion.fingerprint,
        )
        return suggestion, message, key

    async def _live(self, public_id: str, expected_revision: int | None) -> ActionTable:
        row = await self._repository.get_action(public_id)
        if row is None:
            raise ActionNotFoundError(_NOT_FOUND)
        if expected_revision is not None and row.revision != expected_revision:
            raise ActionConflictError(_STALE)
        return row

    async def _load(self, row: ActionTable) -> Action:
        return (await self._repository.load([row]))[0]

    async def _apply_steps(
        self, row: ActionTable, steps: Sequence[StepEdit], listed: list[int], now: datetime
    ) -> None:
        """Update listed steps, add new ones and delete the rest, in the given order."""
        existing = {step.id: step for step in await self._repository.step_rows(row.id)}
        if not set(listed) <= existing.keys():
            raise ActionConflictError("A step does not belong to this action.")
        for position, edit in enumerate(steps):
            if edit.step_id is None:
                self._repository.add_step(row.id, position, edit.text, now if edit.done else None)
                continue
            step = existing[edit.step_id]
            step.position = position
            step.text = edit.text
            _mark(step, edit.done, now)
        for step_id, step in existing.items():
            if step_id not in listed:
                await self._repository.delete_step(step)

    async def accept(self, suggestion_id: int) -> Action:
        """Turn a suggestion into an action; accepting it again returns the same action.

        An action that was accepted and then soft-deleted is restored rather than copied.
        """

        async def run(now: datetime) -> Action:
            suggestion, message, key = await self._suggestion(suggestion_id)
            decision = await self._repository.get_decision(key)
            accepted_id = _accepted_action_id(decision)
            if accepted_id is not None:
                existing = await self._repository.get_action_by_id(accepted_id)
                if existing is not None:
                    if existing.deleted_at_utc is not None:
                        existing.deleted_at_utc = None
                        _touch(existing, now)
                    return await self._load(existing)
            row = await self._repository.add_action(
                ActionTable(
                    public_id=self._id_factory(),
                    title=suggestion.title,
                    ownership=suggestion.ownership.value,
                    status=ActionStatus.OPEN.value,
                    effort=None if suggestion.effort is None else suggestion.effort.value,
                    deadline_text=suggestion.deadline_text,
                    deadline_precision=suggestion.deadline_precision.value,
                    deadline_date=suggestion.deadline_date,
                    deadline_at_utc=suggestion.deadline_at_utc,
                    deadline_timezone=suggestion.deadline_timezone,
                    suggested_target_date=suggestion.suggested_target_date,
                    target_reason=(
                        None if suggestion.target_reason is None else suggestion.target_reason.value
                    ),
                    target_date=suggestion.suggested_target_date,
                    notes="",
                    evidence=suggestion.evidence,
                    created_at_utc=now,
                    updated_at_utc=now,
                    revision=1,
                )
            )
            for position, text in enumerate(suggestion.steps):
                self._repository.add_step(row.id, position, text, None)
            await self._repository.add_source(row.id, message)
            await self._repository.save_decision(
                key, decision=SuggestionState.ACCEPTED, action_id=row.id, decided_at_utc=now
            )
            return await self._load(row)

        return await self._write(run)

    async def accept_into(
        self, suggestion_id: int, public_id: str, expected_revision: int
    ) -> Action:
        """Add a suggestion's message to an existing action as another source.

        Repeating this for the same action returns it unchanged, even with an old revision.
        Nothing exposes this yet: it is reserved for M8's thread continuity.
        """

        async def run(now: datetime) -> Action:
            _, message, key = await self._suggestion(suggestion_id)
            target = await self._live(public_id, None)
            decision = await self._repository.get_decision(key)
            accepted_id = _accepted_action_id(decision)
            if accepted_id == target.id:
                return await self._load(target)
            if accepted_id is not None:
                other = await self._repository.get_action_by_id(accepted_id)
                if other is not None and other.deleted_at_utc is None:
                    raise ActionConflictError("That suggestion already belongs to another action.")
            if target.revision != expected_revision:
                raise ActionConflictError(_STALE)
            await self._repository.add_source(target.id, message)
            await self._repository.save_decision(
                key, decision=SuggestionState.ACCEPTED, action_id=target.id, decided_at_utc=now
            )
            _touch(target, now)
            return await self._load(target)

        return await self._write(run)

    async def dismiss(self, suggestion_id: int) -> None:
        """Hide a suggestion from later briefs; repeating it changes nothing."""

        async def run(now: datetime) -> None:
            _, _, key = await self._suggestion(suggestion_id)
            decision = await self._repository.get_decision(key)
            accepted_id = _accepted_action_id(decision)
            if accepted_id is not None:
                action = await self._repository.get_action_by_id(accepted_id)
                if action is not None and action.deleted_at_utc is None:
                    raise ActionConflictError(
                        "An accepted suggestion can't be dismissed; delete its action instead."
                    )
                if action is not None:
                    return  # Already shown as dismissed; the link lets restore bring it back.
            elif decision is not None and decision.decision == SuggestionState.DISMISSED.value:
                return  # Already dismissed; keep the original time.
            await self._repository.save_decision(
                key, decision=SuggestionState.DISMISSED, action_id=None, decided_at_utc=now
            )

        await self._write(run)

    async def restore_suggestion(self, suggestion_id: int) -> None:
        """Undo a dismissal: the suggestion is pending again, or its deleted action returns."""

        async def run(now: datetime) -> None:
            _, _, key = await self._suggestion(suggestion_id)
            decision = await self._repository.get_decision(key)
            if decision is None:
                return
            accepted_id = _accepted_action_id(decision)
            action = (
                None
                if accepted_id is None
                else await self._repository.get_action_by_id(accepted_id)
            )
            if action is None:
                await self._repository.delete_decision(key)
            elif action.deleted_at_utc is not None:
                action.deleted_at_utc = None
                _touch(action, now)

        await self._write(run)

    async def unaccept(self, public_id: str, expected_revision: int) -> None:
        """Undo an acceptance while the action is still exactly as it was accepted."""

        async def run(now: datetime) -> None:
            row = await self._live(public_id, expected_revision)
            if (
                row.revision != 1
                or row.status != ActionStatus.OPEN.value
                or await self._repository.source_count(row.id) != 1
            ):
                raise ActionConflictError(
                    "Only an unchanged action accepted from one suggestion can be undone."
                )
            await self._repository.delete_decisions_for(row.id)
            await self._repository.delete_action(row)

        await self._write(run)

    async def save(
        self,
        public_id: str,
        expected_revision: int,
        edit: ActionEdit,
        steps: Sequence[StepEdit] | None = None,
    ) -> Action:
        """Apply an edit and, when given, a new plan together, as one revision."""

        async def run(now: datetime) -> Action:
            listed = [] if steps is None else _check_step_list(steps)
            row = await self._live(public_id, expected_revision)
            _apply_edit(row, edit)
            if steps is not None:
                await self._apply_steps(row, steps, listed, now)
            _touch(row, now)
            return await self._load(row)

        return await self._write(run)

    async def complete(self, public_id: str, expected_revision: int) -> Action:
        """Complete an open action; its steps are left as they are."""

        async def run(now: datetime) -> Action:
            row = await self._live(public_id, expected_revision)
            if row.status != ActionStatus.OPEN.value:
                raise ActionConflictError("The action is already completed.")
            row.status = ActionStatus.COMPLETED.value
            row.completed_at_utc = now
            _touch(row, now)
            return await self._load(row)

        return await self._write(run)

    async def reopen(self, public_id: str, expected_revision: int) -> Action:
        """Reopen a completed action."""

        async def run(now: datetime) -> Action:
            row = await self._live(public_id, expected_revision)
            if row.status != ActionStatus.COMPLETED.value:
                raise ActionConflictError("The action is already open.")
            row.status = ActionStatus.OPEN.value
            row.completed_at_utc = None
            _touch(row, now)
            return await self._load(row)

        return await self._write(run)

    async def delete(self, public_id: str, expected_revision: int) -> None:
        """Hide an action; restore() brings it back."""

        async def run(now: datetime) -> None:
            row = await self._live(public_id, expected_revision)
            row.deleted_at_utc = now
            _touch(row, now)

        await self._write(run)

    async def restore(self, public_id: str) -> Action:
        """Bring back a deleted action; an action that isn't deleted is returned as it is."""

        async def run(now: datetime) -> Action:
            row = await self._repository.get_action(public_id, include_deleted=True)
            if row is None:
                raise ActionNotFoundError(_NOT_FOUND)
            if row.deleted_at_utc is not None:
                row.deleted_at_utc = None
                _touch(row, now)
            return await self._load(row)

        return await self._write(run)

    async def get(self, public_id: str) -> Action:
        """A live action; deleted actions are not found."""
        return await self._load(await self._live(public_id, None))

    async def list_actions(
        self, view: ActionFilter, *, limit: int | None = None
    ) -> tuple[Action, ...]:
        """Live actions for a view.

        Open and waiting actions come by target date, then due time, then creation, with
        undated ones last, and are never capped unless ``limit`` is given. Completed actions
        come newest first, at most COMPLETED_LIST_LIMIT unless ``limit`` says otherwise;
        count_actions() says how many there are.
        """
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        if limit is None and view is ActionFilter.COMPLETED:
            limit = COMPLETED_LIST_LIMIT
        actions = await self._repository.load(await self._repository.list_rows(view, limit=limit))
        if view is ActionFilter.COMPLETED:
            return tuple(actions)
        # The rows arrive in ID order and sorted() is stable, so equal keys keep that order.
        ordered = sorted(
            actions,
            key=lambda action: (
                action.target_date or date.max,
                action.due_at_utc() or _LATEST,
                action.created_at_utc,
            ),
        )
        return tuple(ordered if limit is None else ordered[:limit])

    async def count_actions(self, view: ActionFilter) -> int:
        """How many live actions a view holds, however many list_actions() shows."""
        return await self._repository.count_rows(view)
