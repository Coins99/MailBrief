"""Accept, dismiss and manage the owner's actions; every change is one committed revision.

Nothing here reads or sends email: suggestions come from stored analyses. Errors carry
static messages, and nothing logs action text.
"""

import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
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
    ThreadLink,
)
from mailbrief.domain.analysis import (
    ActionOwnership,
    ActionSuggestion,
    DeadlinePrecision,
    deadline_due_at,
)
from mailbrief.domain.common import normalize_utc
from mailbrief.domain.messages import ProviderKind
from mailbrief.storage.actions import ActionRepository, DecisionKey, suggestion_from_row
from mailbrief.storage.tables import (
    AccountTable,
    ActionStepTable,
    ActionTable,
    MessageTable,
    SuggestionDecisionTable,
)

_NOT_FOUND: Final = "That action was not found."
_SUGGESTION_NOT_FOUND: Final = "That suggestion was not found."
_STALE: Final = "The action changed since it was loaded; reload it and try again."
_UNDO_ADD: Final = "Only an unchanged addition can be undone."
_LATEST: Final = datetime.max.replace(tzinfo=UTC)
COMPLETED_LIST_LIMIT: Final = 200  # The newest completed actions a list shows by default.
MAX_THREAD_LINKS: Final = 3  # The actions named for one email, most urgent first.


class ActionNotFoundError(LookupError):
    """No live action has that public ID."""


class SuggestionNotFoundError(LookupError):
    """No stored suggestion has that ID, or its row no longer validates."""


class ActionConflictError(ValueError):
    """The action changed since it was read, or the change is not allowed now."""


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    """A suggestion's decision as it was before accept_into, for its undo to put back:
    ``decision`` is None when there was none (the suggestion was pending)."""

    decision: SuggestionState | None = None
    action_id: int | None = None
    decided_at_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class AcceptedInto:
    """What accept_into did: the action, whether the email became a new source of it,
    whether anything changed at all (repeating an addition changes nothing, and so has
    nothing to undo), and the decision it replaced."""

    action: Action
    source_added: bool
    changed: bool
    previous: DecisionSnapshot


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_public_id() -> str:
    return str(uuid.uuid4())


def urgency(
    target_date: date | None, due_at_utc: datetime | None, created_at_utc: datetime
) -> tuple[date, datetime, datetime]:
    """How open actions are ordered: by target date, then due time, then creation, with
    undated actions last."""
    return (target_date or date.max, due_at_utc or _LATEST, created_at_utc)


def touch(row: ActionTable, now: datetime) -> None:
    """Record a change to an action: one more revision, at ``now``."""
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
    ) -> tuple[ActionSuggestion, MessageTable, AccountTable, DecisionKey]:
        """The suggestion, its message and account, and the key its decision is kept under."""
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
        return suggestion, message, account, key

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
            suggestion, message, account, key = await self._suggestion(suggestion_id)
            decision = await self._repository.get_decision(key)
            accepted_id = _accepted_action_id(decision)
            if accepted_id is not None:
                existing = await self._repository.get_action_by_id(accepted_id)
                if existing is not None:
                    if existing.deleted_at_utc is not None:
                        existing.deleted_at_utc = None
                        touch(existing, now)
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
            await self._repository.add_source(row.id, message, account)
            await self._repository.save_decision(
                key, decision=SuggestionState.ACCEPTED, action_id=row.id, decided_at_utc=now
            )
            return await self._load(row)

        return await self._write(run)

    async def accept_into(
        self, suggestion_id: int, public_id: str, expected_revision: int
    ) -> AcceptedInto:
        """Accept a suggestion into an existing action instead of creating another, adding
        its email as a source unless it already is one; one revision.

        Only a live, open action takes it: a completed or deleted one raises
        ActionConflictError, so it is reopened first. The brief offers this for the open
        actions that track the email's thread; naming the action (``actions accept N
        --into``) allows any thread or account. Repeating it for the same action returns
        the action unchanged, even with an old revision, with ``changed`` false: there is
        nothing to undo then. Otherwise ``previous`` is the decision it replaced (none, a
        dismissal, or an acceptance into a deleted action), which undo_accept_into() puts
        back while the action is unchanged.
        """

        async def run(now: datetime) -> AcceptedInto:
            _, message, account, key = await self._suggestion(suggestion_id)
            target = await self._repository.get_action(public_id, include_deleted=True)
            if target is None:
                raise ActionNotFoundError(_NOT_FOUND)
            if target.deleted_at_utc is not None or target.status != ActionStatus.OPEN.value:
                raise ActionConflictError("Reopen the action before adding to it.")
            decision = await self._repository.get_decision(key)
            accepted_id = _accepted_action_id(decision)
            previous = (
                DecisionSnapshot()
                if decision is None
                else DecisionSnapshot(
                    decision=SuggestionState(decision.decision),
                    action_id=decision.action_id,
                    decided_at_utc=decision.decided_at_utc,
                )
            )
            if accepted_id == target.id:
                return AcceptedInto(
                    await self._load(target), source_added=False, changed=False, previous=previous
                )
            if accepted_id is not None:
                other = await self._repository.get_action_by_id(accepted_id)
                if other is not None and other.deleted_at_utc is None:
                    raise ActionConflictError("That suggestion already belongs to another action.")
            if target.revision != expected_revision:
                raise ActionConflictError(_STALE)
            added = await self._repository.add_source(target.id, message, account)
            await self._repository.save_decision(
                key, decision=SuggestionState.ACCEPTED, action_id=target.id, decided_at_utc=now
            )
            touch(target, now)
            return AcceptedInto(
                await self._load(target), source_added=added, changed=True, previous=previous
            )

        return await self._write(run)

    async def undo_accept_into(
        self,
        suggestion_id: int,
        public_id: str,
        expected_revision: int,
        remove_source: bool,
        *,
        previous: DecisionSnapshot,
    ) -> Action:
        """Reverse accept_into while the action is exactly as it left it; one revision.

        The suggestion's decision becomes exactly ``previous`` (accept_into's): none, so it
        is pending again, or the earlier dismissal or acceptance with its original time.
        With ``remove_source`` (accept_into's ``source_added``), its email stops being a
        source, but an action's last source always stays. Raises ActionConflictError unless
        the action is live, still at ``expected_revision``, and the suggestion is still
        accepted into it.
        """

        async def run(now: datetime) -> Action:
            _, message, _, key = await self._suggestion(suggestion_id)
            row = await self._repository.get_action(public_id)
            decision = await self._repository.get_decision(key)
            if (
                row is None
                or row.revision != expected_revision
                or _accepted_action_id(decision) != row.id
            ):
                raise ActionConflictError(_UNDO_ADD)
            if previous.decision is None or previous.decided_at_utc is None:
                await self._repository.delete_decision(key)
            else:
                await self._repository.save_decision(
                    key,
                    decision=previous.decision,
                    action_id=previous.action_id,
                    decided_at_utc=previous.decided_at_utc,
                )
            if remove_source and await self._repository.source_count(row.id) > 1:
                await self._repository.remove_source(row.id, message.provider_message_id)
            touch(row, now)
            return await self._load(row)

        return await self._write(run)

    async def dismiss(self, suggestion_id: int) -> None:
        """Hide a suggestion from later briefs; repeating it changes nothing."""

        async def run(now: datetime) -> None:
            _, _, _, key = await self._suggestion(suggestion_id)
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
            _, _, _, key = await self._suggestion(suggestion_id)
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
                touch(action, now)

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
            touch(row, now)
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
            touch(row, now)
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
            touch(row, now)
            return await self._load(row)

        return await self._write(run)

    async def delete(self, public_id: str, expected_revision: int) -> None:
        """Hide an action; restore() brings it back."""

        async def run(now: datetime) -> None:
            row = await self._live(public_id, expected_revision)
            row.deleted_at_utc = now
            touch(row, now)

        await self._write(run)

    async def restore(self, public_id: str) -> Action:
        """Bring back a deleted action; an action that isn't deleted is returned as it is."""

        async def run(now: datetime) -> Action:
            row = await self._repository.get_action(public_id, include_deleted=True)
            if row is None:
                raise ActionNotFoundError(_NOT_FOUND)
            if row.deleted_at_utc is not None:
                row.deleted_at_utc = None
                touch(row, now)
            return await self._load(row)

        return await self._write(run)

    async def mark_thread_seen(self, public_id: str, expected_revision: int) -> Action:
        """Move the owner's "seen" watermark past the latest message in the action's threads
        (ADR 0015), from others or the owner.

        With nothing new, the action is returned unchanged, without a new revision. This is
        the only thing that changes an action because of its threads.
        """

        async def run(now: datetime) -> Action:
            row = await self._live(public_id, expected_revision)
            action = await self._load(row)
            thread = action.thread
            if thread is None or not thread.unseen:
                return action
            seen = [
                at for at in (thread.latest_at_utc, thread.owner_replied_at_utc) if at is not None
            ]
            row.thread_seen_until_utc = max(seen)
            touch(row, now)
            return await self._load(row)

        return await self._write(run)

    async def thread_links(
        self, account_email: str, message_keys: Iterable[str]
    ) -> dict[str, tuple[ThreadLink, ...]]:
        """The live, open actions that continue each email's thread (ADR 0015).

        ``message_keys`` are Gmail message IDs of ``account_email``'s cached messages. An
        action continues a message's thread when one of its sources has the same provider,
        account and thread snapshot. Each message gets at most MAX_THREAD_LINKS, most urgent
        first; one that is not cached, has no thread or has no such action gets no entry.
        The number of queries does not grow with the number of messages or actions.
        """
        rows = await self._repository.thread_link_rows(
            ProviderKind.GMAIL.value, account_email, message_keys
        )
        found: dict[str, dict[int, tuple[tuple[date, datetime, datetime, int], ThreadLink]]] = {}
        for row in rows:
            due = deadline_due_at(
                DeadlinePrecision(row.deadline_precision),
                row.deadline_date,
                row.deadline_at_utc,
                row.deadline_timezone,
            )
            links = found.setdefault(row.message_key, {})
            earlier = links.get(row.action_id)
            links[row.action_id] = (
                (*urgency(row.target_date, due, row.created_at_utc), row.action_id),
                ThreadLink(
                    public_id=row.public_id,
                    title=row.title,
                    revision=row.revision,
                    ownership=ActionOwnership(row.ownership),
                    is_source=row.is_source or (earlier is not None and earlier[1].is_source),
                ),
            )
        ranked: dict[str, tuple[ThreadLink, ...]] = {}
        for key, links in found.items():
            ordered = sorted(links.values(), key=lambda pair: pair[0])
            ranked[key] = tuple(link for _, link in ordered[:MAX_THREAD_LINKS])
        return ranked

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
            key=lambda action: urgency(
                action.target_date, action.due_at_utc(), action.created_at_utc
            ),
        )
        return tuple(ordered if limit is None else ordered[:limit])

    async def count_actions(self, view: ActionFilter) -> int:
        """How many live actions a view holds, however many list_actions() shows."""
        return await self._repository.count_rows(view)
