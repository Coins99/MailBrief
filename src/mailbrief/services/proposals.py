"""Follow-up proposals: derived from a brief's analyses, applied only by the owner (ADR 0016).

derive() stores proposals and never changes an action. apply() and undo_apply() are the only
changes a proposal makes, one action revision each, and never touch an action's title,
notes, steps or ownership. Errors carry static messages, and nothing logs mail or action
text.
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import Action, ActionProposal, ActionStatus, ProposalState
from mailbrief.domain.analysis import (
    DeadlinePrecision,
    FollowUpKind,
    MessageAnalysis,
    deadline_due_at,
)
from mailbrief.domain.common import normalize_utc, utc_now
from mailbrief.domain.messages import NormalizedMessage, ProviderKind, is_own_message
from mailbrief.services.actions import ActionConflictError, touch, urgency
from mailbrief.services.analysis import PlannedMessage
from mailbrief.services.deadlines import ResolvedDeadline, suggest_target_for
from mailbrief.services.owned import OwnedRecordService
from mailbrief.services.threads import own_addresses
from mailbrief.storage.actions import ActionRepository
from mailbrief.storage.proposals import ProposalRepository, proposal_from_row
from mailbrief.storage.tables import (
    AccountTable,
    ActionProposalTable,
    ActionSourceTable,
    ActionTable,
    MessageTable,
)

logger = logging.getLogger(__name__)

MAX_PROPOSALS_PER_EMAIL: Final = 3
PROPOSAL_LIST_LIMIT: Final = 100
_NOT_FOUND: Final = "That proposal was not found."
_DECIDED: Final = "That proposal was already applied or dismissed."
_NOT_OPEN: Final = "The action is no longer open, so the proposal can't be applied."
_STALE: Final = "The action changed since it was loaded; reload it and try again."
_UNDO: Final = "Only an unchanged update can be undone."
_APPLIED: Final = "That proposal was applied; undo it instead."
# The action fields applying may change, restored by Undo.
_DATE_FIELDS: Final = ("deadline_date", "suggested_target_date", "target_date")
_TIME_FIELDS: Final = ("deadline_at_utc", "completed_at_utc")
_TEXT_FIELDS: Final = (
    "deadline_text",
    "deadline_precision",
    "deadline_timezone",
    "target_reason",
    "status",
)


class ProposalNotFoundError(LookupError):
    """No proposal has that ID."""

    def __init__(self) -> None:
        super().__init__(_NOT_FOUND)


def _due(action: ActionTable) -> datetime | None:
    return deadline_due_at(
        DeadlinePrecision(action.deadline_precision),
        action.deadline_date,
        action.deadline_at_utc,
        action.deadline_timezone,
    )


def _same_deadline(action: ActionTable, analysis: MessageAnalysis) -> bool:
    """Whether applying the email's deadline would leave the action's as it is.

    Dated deadlines compare by day or instant and zone; an unresolved one by its words.
    """
    precision = analysis.deadline_precision
    if action.deadline_precision != precision.value:
        return False
    if precision is DeadlinePrecision.UNRESOLVED:
        return action.deadline_text == analysis.deadline_text
    return (action.deadline_date, action.deadline_at_utc, action.deadline_timezone) == (
        analysis.deadline_date,
        analysis.deadline_at_utc,
        analysis.deadline_timezone,
    )


def _previous(action: ActionTable) -> dict[str, str | None]:
    """The fields applying may change, as JSON-safe text."""
    saved: dict[str, str | None] = {name: getattr(action, name) for name in _TEXT_FIELDS}
    for name in (*_DATE_FIELDS, *_TIME_FIELDS):
        value: date | datetime | None = getattr(action, name)
        saved[name] = None if value is None else value.isoformat()
    return saved


def _restore(action: ActionTable, saved: dict[str, str | None]) -> None:
    for name in _TEXT_FIELDS:
        setattr(action, name, saved[name])
    for name in _DATE_FIELDS:
        text = saved[name]
        setattr(action, name, None if text is None else date.fromisoformat(text))
    for name in _TIME_FIELDS:
        text = saved[name]
        setattr(action, name, None if text is None else datetime.fromisoformat(text))


def _new_row(
    action: ActionTable,
    account: AccountTable,
    message: NormalizedMessage,
    message_row_id: int | None,
    analysis: MessageAnalysis,
    timezone_name: str,
    now: datetime,
) -> ActionProposalTable:
    """A pending proposal with the email's snapshot, and the deadline for a new deadline."""
    assert analysis.follow_up_evidence is not None
    row = ActionProposalTable(
        action_id=action.id,
        message_id=message_row_id,
        kind=analysis.follow_up.value,
        state=ProposalState.PENDING.value,
        deadline_precision=DeadlinePrecision.NONE.value,
        evidence=analysis.follow_up_evidence,
        provider=account.provider,
        provider_account_id=account.provider_account_id,
        provider_message_id=message.provider_message_id,
        provider_thread_id=message.conversation_id,
        subject=message.subject,
        sender_address=message.sender.address,
        web_link=str(message.web_link),
        received_at_utc=message.received_at_utc,
        source_added=False,
        created_at_utc=now,
    )
    if analysis.follow_up is FollowUpKind.NEW_DEADLINE:
        deadline = ResolvedDeadline(
            analysis.deadline_text,
            analysis.deadline_precision,
            analysis.deadline_date,
            analysis.deadline_at_utc,
            analysis.deadline_timezone,
        )
        target, reason = suggest_target_for(deadline, message.received_at_utc, timezone_name)
        row.deadline_text = deadline.text
        row.deadline_precision = deadline.precision.value
        row.deadline_date = deadline.date
        row.deadline_at_utc = deadline.at_utc
        row.deadline_timezone = deadline.timezone
        row.suggested_target_date = target
        row.target_reason = None if reason is None else reason.value
    return row


class ProposalService(OwnedRecordService):
    """Stores, lists and decides follow-up proposals.

    Every mutating method reads the clock once, commits once and rolls back on failure.
    """

    def __init__(self, session: AsyncSession, *, clock: Callable[[], datetime] = utc_now) -> None:
        super().__init__(session, clock=clock)
        self._proposals = ProposalRepository(session)
        self._actions = ActionRepository(session)

    def _stale_error(self) -> Exception:
        return ActionConflictError(_STALE)

    async def _row(self, proposal_id: int) -> ActionProposalTable:
        row = await self._proposals.get(proposal_id)
        if row is None:
            raise ProposalNotFoundError()
        return row

    async def _action(self, row: ActionProposalTable) -> ActionTable:
        action = await self._session.get(ActionTable, row.action_id)
        assert action is not None  # A proposal goes with its action.
        return action

    async def _load(self, action: ActionTable) -> Action:
        return (await self._actions.load([action]))[0]

    async def derive(
        self, account: AccountTable, planned: Sequence[PlannedMessage], timezone_name: str
    ) -> int:
        """Store pending proposals for a brief's analyzed messages; returns how many.

        A message with a follow-up signal and a thread proposes an update to each live, open
        action of the account with a source in that thread, when the action's latest source
        there is older than the message and the message isn't already a source, most urgent
        first. An email proposes to at most MAX_PROPOSALS_PER_EMAIL actions ever: the actions
        that already have a proposal from it, in any state and even if since closed, use up
        slots, and only the remaining ones are filled. A new deadline equal to the action's
        proposes nothing and uses no slot. A proposal for the same action, email and kind, in
        any state, is never made again; a pending one of another kind is replaced. Cached
        analyses count like new ones. The owner's own messages (sent, or from one of the
        account's addresses) propose nothing. Actions are never changed.
        """
        mine = own_addresses(account)
        signals = [
            (item.ranked.message, item.analysis, item.message_row_id)
            for item in planned
            if item.analysis is not None
            and item.analysis.follow_up is not FollowUpKind.NONE
            and item.ranked.message.conversation_id is not None
            and not is_own_message(item.ranked.message, mine)
        ]
        if not signals:
            return 0

        async def run(now: datetime) -> int:
            threads = {message.conversation_id for message, _, _ in signals}
            sources = await self._proposals.thread_sources(
                account.provider,
                account.provider_account_id,
                (thread for thread in threads if thread is not None),
            )
            # Each thread's actions with their latest source there.
            latest: dict[str, dict[int, tuple[ActionTable, datetime]]] = {}
            for source in sources:
                by_action = latest.setdefault(source.thread_id, {})
                known = by_action.get(source.action.id)
                if known is None or source.received_at_utc > known[1]:
                    by_action[source.action.id] = (source.action, source.received_at_utc)
            action_ids = {source.action.id for source in sources}
            keys = {message.provider_message_id for message, _, _ in signals}
            already = await self._proposals.sources_among(action_ids, keys)
            existing = await self._proposals.for_actions_and_messages(action_ids, keys)
            proposed = await self._proposals.proposed_actions(
                account.provider, account.provider_account_id, keys
            )
            created = 0
            for message, analysis, message_row_id in signals:
                assert analysis is not None and message.conversation_id is not None
                key = message.provider_message_id
                received = normalize_utc(message.received_at_utc)
                candidates = sorted(
                    (
                        action
                        for action, since in latest.get(message.conversation_id, {}).values()
                        if since < received and (action.id, key) not in already
                    ),
                    key=lambda action: (
                        *urgency(action.target_date, _due(action), action.created_at_utc),
                        action.id,
                    ),
                )
                slots = MAX_PROPOSALS_PER_EMAIL - len(proposed.get(key, ()))
                for action in candidates:
                    rows = existing.get((action.id, key), [])
                    if not rows and slots <= 0:
                        continue  # This email has proposed to as many actions as it may.
                    if analysis.follow_up is FollowUpKind.NEW_DEADLINE and _same_deadline(
                        action, analysis
                    ):
                        continue
                    if any(row.kind == analysis.follow_up.value for row in rows):
                        continue  # Made before; applied or dismissed ones never come back.
                    for row in rows:
                        if row.state == ProposalState.PENDING.value:
                            await self._proposals.delete(row)  # A newer analysis replaces it.
                    self._proposals.add(
                        _new_row(
                            action, account, message, message_row_id, analysis, timezone_name, now
                        )
                    )
                    created += 1
                    if not rows:
                        slots -= 1
            return created

        created = await self._write(run)
        logger.info("Follow-up proposals: %d created from %d signals", created, len(signals))
        return created

    async def get(self, proposal_id: int) -> ActionProposal:
        """A proposal in any state."""
        row = await self._row(proposal_id)
        action = await self._action(row)
        return proposal_from_row(row, action.public_id, action.title, action.revision)

    async def pending(self, limit: int = PROPOSAL_LIST_LIMIT) -> tuple[ActionProposal, ...]:
        """Pending proposals of live, open actions, newest first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        rows = await self._proposals.pending(limit)
        return tuple(
            proposal_from_row(row, public_id, title, revision)
            for row, public_id, title, revision in rows
        )

    async def pending_for_messages(
        self, account_email: str, message_keys: Iterable[str]
    ) -> dict[str, tuple[ActionProposal, ...]]:
        """Pending proposals of live, open actions made from these Gmail messages of the
        account, by message ID; a message without any has no entry."""
        rows = await self._proposals.pending_for_messages(
            ProviderKind.GMAIL.value, account_email, message_keys
        )
        found: dict[str, list[ActionProposal]] = {}
        for row, public_id, title, revision in rows:
            found.setdefault(row.provider_message_id, []).append(
                proposal_from_row(row, public_id, title, revision)
            )
        return {key: tuple(proposals) for key, proposals in found.items()}

    async def apply(self, proposal_id: int, expected_revision: int) -> Action:
        """Apply a pending proposal to its live, open action, as one revision.

        A new deadline sets the deadline and the suggested target, and moves the target
        date only when it still equals the old suggested target; a cancellation or delivery
        completes the action. The email becomes a source, from local mail or else from the
        snapshot. What changed is kept for undo_apply(). Raises ActionConflictError unless
        the proposal is pending and the action is open and at ``expected_revision``.
        """

        async def run(now: datetime) -> Action:
            row = await self._row(proposal_id)
            if row.state != ProposalState.PENDING.value:
                raise ActionConflictError(_DECIDED)
            action = await self._action(row)
            if action.deleted_at_utc is not None or action.status != ActionStatus.OPEN.value:
                raise ActionConflictError(_NOT_OPEN)
            if action.revision != expected_revision:
                raise ActionConflictError(_STALE)
            row.previous_json = _previous(action)
            if row.kind == FollowUpKind.NEW_DEADLINE.value:
                if action.target_date == action.suggested_target_date:
                    action.target_date = row.suggested_target_date  # The owner never moved it.
                action.deadline_text = row.deadline_text
                action.deadline_precision = row.deadline_precision
                action.deadline_date = row.deadline_date
                action.deadline_at_utc = row.deadline_at_utc
                action.deadline_timezone = row.deadline_timezone
                action.suggested_target_date = row.suggested_target_date
                action.target_reason = row.target_reason
            else:
                action.status = ActionStatus.COMPLETED.value
                action.completed_at_utc = now
            row.source_added = await self._add_source(action, row)
            row.state = ProposalState.APPLIED.value
            row.decided_at_utc = now
            touch(action, now)
            row.applied_revision = action.revision
            return await self._load(action)

        return await self._write(run)

    async def _add_source(self, action: ActionTable, row: ActionProposalTable) -> bool:
        """Make the proposal's email a source; False when it already was one."""
        found = await self._session.execute(
            select(MessageTable, AccountTable)
            .join(AccountTable, MessageTable.account_id == AccountTable.id)
            .where(
                AccountTable.provider == row.provider,
                AccountTable.provider_account_id == row.provider_account_id,
                MessageTable.provider_message_id == row.provider_message_id,
            )
        )
        cached = found.tuples().first()
        if cached is not None:
            return await self._actions.add_source(action.id, cached[0], cached[1])
        return await self._actions.add_source_snapshot(
            ActionSourceTable(
                action_id=action.id,
                message_id=None,
                provider_message_id=row.provider_message_id,
                subject=row.subject,
                sender_address=row.sender_address,
                web_link=row.web_link,
                received_at_utc=row.received_at_utc,
                provider=row.provider,
                provider_account_id=row.provider_account_id,
                provider_thread_id=row.provider_thread_id,
            )
        )

    async def undo_apply(self, proposal_id: int, expected_revision: int) -> Action:
        """Reverse apply() while the action is exactly as it left it, as one revision.

        The action's previous deadline, targets and status come back, the source the apply
        added goes unless it is the action's last, and the proposal is pending again.
        Raises ActionConflictError otherwise.
        """

        async def run(now: datetime) -> Action:
            row = await self._row(proposal_id)
            action = await self._action(row)
            saved = row.previous_json
            if (
                row.state != ProposalState.APPLIED.value
                or saved is None
                or action.deleted_at_utc is not None
                or action.revision != expected_revision
                or row.applied_revision != expected_revision
            ):
                raise ActionConflictError(_UNDO)
            _restore(action, saved)
            if row.source_added and await self._actions.source_count(action.id) > 1:
                await self._actions.remove_source(action.id, row.provider_message_id)
            row.state = ProposalState.PENDING.value
            row.previous_json = None
            row.source_added = False
            row.applied_revision = None
            row.decided_at_utc = None
            touch(action, now)
            return await self._load(action)

        return await self._write(run)

    async def dismiss(self, proposal_id: int) -> None:
        """Dismiss a pending proposal; it is never proposed again. No action revision."""

        async def run(now: datetime) -> None:
            row = await self._row(proposal_id)
            if row.state == ProposalState.APPLIED.value:
                raise ActionConflictError(_APPLIED)
            if row.state == ProposalState.PENDING.value:
                row.state = ProposalState.DISMISSED.value
                row.decided_at_utc = now

        await self._write(run)

    async def restore(self, proposal_id: int) -> None:
        """Make a dismissed proposal pending again. No action revision."""

        async def run(now: datetime) -> None:
            row = await self._row(proposal_id)
            if row.state == ProposalState.APPLIED.value:
                raise ActionConflictError(_APPLIED)
            if row.state == ProposalState.DISMISSED.value:
                row.state = ProposalState.PENDING.value
                row.decided_at_utc = None

        await self._write(run)
