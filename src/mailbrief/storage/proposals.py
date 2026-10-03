"""Follow-up proposals for actions (ADR 0016), and the queries that derive them.

Rows change only through ORM objects, never bulk statements. actions.py imports this module,
so it must never import actions.py.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import HttpUrl
from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import ActionProposal, ActionStatus, ProposalState
from mailbrief.domain.analysis import DeadlinePrecision, FollowUpKind, TargetReason
from mailbrief.storage.database import chunked
from mailbrief.storage.tables import (
    AccountTable,
    ActionProposalTable,
    ActionSourceTable,
    ActionTable,
)


def proposal_from_row(
    row: ActionProposalTable, public_id: str, title: str, revision: int
) -> ActionProposal:
    """Rebuild a stored proposal for the action with ``public_id``, ``title`` and, as loaded,
    ``revision``."""
    return ActionProposal(
        id=row.id,
        action_public_id=public_id,
        action_title=title,
        action_revision=revision,
        kind=FollowUpKind(row.kind),
        state=ProposalState(row.state),
        deadline_text=row.deadline_text,
        deadline_precision=DeadlinePrecision(row.deadline_precision),
        deadline_date=row.deadline_date,
        deadline_at_utc=row.deadline_at_utc,
        deadline_timezone=row.deadline_timezone,
        suggested_target_date=row.suggested_target_date,
        target_reason=None if row.target_reason is None else TargetReason(row.target_reason),
        evidence=row.evidence,
        provider_message_id=row.provider_message_id,
        subject=row.subject,
        sender_address=row.sender_address,
        received_at_utc=row.received_at_utc,
        web_link=HttpUrl(row.web_link),
        created_at_utc=row.created_at_utc,
    )


@dataclass(frozen=True, slots=True)
class ThreadSource:
    """A live, open action and one of its sources in a thread being looked at."""

    action: ActionTable
    thread_id: str
    provider_message_id: str
    received_at_utc: datetime


def _live_open() -> tuple[ColumnElement[bool], ...]:
    """The conditions for a live, open action."""
    return (ActionTable.deleted_at_utc.is_(None), ActionTable.status == ActionStatus.OPEN.value)


class ProposalRepository:
    """Proposals and the actions and sources they are derived from."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, proposal_id: int) -> ActionProposalTable | None:
        return await self._session.get(ActionProposalTable, proposal_id)

    def add(self, row: ActionProposalTable) -> None:
        self._session.add(row)

    async def delete(self, row: ActionProposalTable) -> None:
        await self._session.delete(row)

    async def thread_sources(
        self, provider: str, provider_account_id: str, thread_ids: Iterable[str]
    ) -> list[ThreadSource]:
        """Every source in these threads of the account's live, open actions."""
        found: list[ThreadSource] = []
        for chunk in chunked(thread_ids):
            result = await self._session.execute(
                select(
                    ActionTable,
                    ActionSourceTable.provider_thread_id,
                    ActionSourceTable.provider_message_id,
                    ActionSourceTable.received_at_utc,
                )
                .join(ActionSourceTable, ActionSourceTable.action_id == ActionTable.id)
                .where(
                    *_live_open(),
                    ActionSourceTable.provider == provider,
                    ActionSourceTable.provider_account_id == provider_account_id,
                    ActionSourceTable.provider_thread_id.in_(chunk),
                )
                .order_by(ActionTable.id, ActionSourceTable.id)
            )
            for action, thread_id, message_id, received in result.tuples():
                assert thread_id is not None
                found.append(ThreadSource(action, thread_id, message_id, received))
        return found

    async def sources_among(
        self, action_ids: Iterable[int], provider_message_ids: Iterable[str]
    ) -> set[tuple[int, str]]:
        """(action ID, provider message ID) for these actions' sources among these emails."""
        messages = set(provider_message_ids)
        found: set[tuple[int, str]] = set()
        for chunk in chunked(action_ids):
            for message_chunk in chunked(messages):
                result = await self._session.execute(
                    select(
                        ActionSourceTable.action_id, ActionSourceTable.provider_message_id
                    ).where(
                        ActionSourceTable.action_id.in_(chunk),
                        ActionSourceTable.provider_message_id.in_(message_chunk),
                    )
                )
                found.update(result.tuples())
        return found

    async def proposed_actions(
        self, provider: str, provider_account_id: str, provider_message_ids: Iterable[str]
    ) -> dict[str, set[int]]:
        """For each of these emails of the account, the IDs of the actions that have a
        proposal from it in any state, whether or not the action is still live and open."""
        found: dict[str, set[int]] = {}
        for chunk in chunked(provider_message_ids):
            result = await self._session.execute(
                select(
                    ActionProposalTable.provider_message_id, ActionProposalTable.action_id
                ).where(
                    ActionProposalTable.provider == provider,
                    ActionProposalTable.provider_account_id == provider_account_id,
                    ActionProposalTable.provider_message_id.in_(chunk),
                )
            )
            for message_id, action_id in result.tuples():
                found.setdefault(message_id, set()).add(action_id)
        return found

    async def for_actions_and_messages(
        self, action_ids: Iterable[int], provider_message_ids: Iterable[str]
    ) -> dict[tuple[int, str], list[ActionProposalTable]]:
        """Proposals in any state for these actions and emails, by (action ID, email)."""
        messages = set(provider_message_ids)
        found: dict[tuple[int, str], list[ActionProposalTable]] = {}
        for chunk in chunked(action_ids):
            for message_chunk in chunked(messages):
                result = await self._session.scalars(
                    select(ActionProposalTable)
                    .where(
                        ActionProposalTable.action_id.in_(chunk),
                        ActionProposalTable.provider_message_id.in_(message_chunk),
                    )
                    .order_by(ActionProposalTable.id)
                )
                for row in result:
                    found.setdefault((row.action_id, row.provider_message_id), []).append(row)
        return found

    async def pending_for_actions(
        self, action_ids: Sequence[int]
    ) -> dict[int, list[ActionProposalTable]]:
        """Pending proposals of these actions, oldest first, by action ID."""
        found: dict[int, list[ActionProposalTable]] = {}
        for chunk in chunked(action_ids):
            result = await self._session.scalars(
                select(ActionProposalTable)
                .where(
                    ActionProposalTable.action_id.in_(chunk),
                    ActionProposalTable.state == ProposalState.PENDING.value,
                )
                .order_by(ActionProposalTable.action_id, ActionProposalTable.id)
            )
            for row in result:
                found.setdefault(row.action_id, []).append(row)
        return found

    async def pending(self, limit: int) -> list[tuple[ActionProposalTable, str, str, int]]:
        """Pending proposals of live, open actions, newest first, with each action's public
        ID, title and revision."""
        result = await self._session.execute(
            select(
                ActionProposalTable,
                ActionTable.public_id,
                ActionTable.title,
                ActionTable.revision,
            )
            .join(ActionTable, ActionProposalTable.action_id == ActionTable.id)
            .where(ActionProposalTable.state == ProposalState.PENDING.value, *_live_open())
            .order_by(ActionProposalTable.created_at_utc.desc(), ActionProposalTable.id.desc())
            .limit(limit)
        )
        return list(result.tuples())

    async def pending_for_messages(
        self, provider: str, account_email: str, provider_message_ids: Iterable[str]
    ) -> list[tuple[ActionProposalTable, str, str, int]]:
        """Pending proposals of live, open actions made from these emails of the account,
        with each action's public ID, title and revision, oldest first."""
        found: list[tuple[ActionProposalTable, str, str, int]] = []
        for chunk in chunked(provider_message_ids):
            result = await self._session.execute(
                select(
                    ActionProposalTable,
                    ActionTable.public_id,
                    ActionTable.title,
                    ActionTable.revision,
                )
                .join(ActionTable, ActionProposalTable.action_id == ActionTable.id)
                .join(
                    AccountTable,
                    and_(
                        AccountTable.provider == ActionProposalTable.provider,
                        AccountTable.provider_account_id == ActionProposalTable.provider_account_id,
                    ),
                )
                .where(
                    AccountTable.provider == provider,
                    AccountTable.email_address == account_email,
                    ActionProposalTable.provider_message_id.in_(chunk),
                    ActionProposalTable.state == ProposalState.PENDING.value,
                    *_live_open(),
                )
                .order_by(ActionProposalTable.id)
            )
            found.extend(result.tuples())
        return found
