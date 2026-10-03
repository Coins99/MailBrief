"""Follow-up proposals: derived from analyses, applied, undone, dismissed and restored."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import Action, ActionEdit, ActionStatus, ProposalState, StepEdit
from mailbrief.domain.analysis import (
    ActionOwnership,
    DeadlinePrecision,
    FollowUpKind,
    MessageAnalysis,
    TargetReason,
)
from mailbrief.domain.briefs import AnalysisOutcome
from mailbrief.domain.messages import (
    AccountIdentity,
    EmailContact,
    NormalizedMessage,
    ProviderKind,
    RankedMessage,
)
from mailbrief.services.actions import ActionConflictError, ActionService
from mailbrief.services.analysis import PlannedMessage
from mailbrief.services.proposals import (
    MAX_PROPOSALS_PER_EMAIL,
    ProposalNotFoundError,
    ProposalService,
)
from mailbrief.storage.actions import ActionRepository
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, AnalysisRepository, MessageRepository
from mailbrief.storage.tables import (
    AccountTable,
    ActionProposalTable,
    ActionSourceTable,
    ActionTable,
    MessageTable,
)
from tests.factories import make_analysis, make_message

ZONE = "America/Toronto"
SOURCE_AT = datetime(2026, 9, 28, 12, tzinfo=UTC)  # A Monday.
REPLY_AT = datetime(2026, 9, 30, 13, tzinfo=UTC)
NOW = datetime(2026, 9, 30, 15, tzinfo=UTC)
MOVE = "Can we move this to next Monday?"
FRIDAY: dict[str, Any] = {
    "deadline_text": "Friday",
    "deadline_precision": "date",
    "deadline_date": date(2026, 10, 2),
    "deadline_timezone": ZONE,
    "suggested_target_date": date(2026, 10, 1),
    "target_reason": "working_day_before",
    "target_date": date(2026, 10, 1),
}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    database = Database.from_path(tmp_path / "proposals.sqlite3")
    await database.create_schema_for_tests()
    try:
        async with database.session() as active:
            yield active
    finally:
        await database.dispose()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(session: AsyncSession, clock: Clock) -> ProposalService:
    return ProposalService(session, clock=clock)


async def account(session: AsyncSession, provider_account_id: str = "gmail-1") -> AccountTable:
    row = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.GMAIL,
            provider_account_id=provider_account_id,
            email_address=f"{provider_account_id}@example.com",
        )
    )
    await session.commit()
    return row


def email(
    key: str, thread: str | None = "deck", at: datetime = REPLY_AT, owner: str = "gmail-1"
) -> NormalizedMessage:
    return make_message(
        provider=ProviderKind.GMAIL,
        provider_account_id=owner,
        provider_message_id=key,
        conversation_id=thread,
        subject=f"Re: {key}",
        sender=EmailContact(name="Sam", address="sam@example.com"),
        received_at_utc=at,
        web_link=f"https://mail.google.com/mail/u/#all/{key}",
    )


async def cache(session: AsyncSession, owner: AccountTable, message: NormalizedMessage) -> int:
    (row,) = await MessageRepository(session).upsert_messages(owner.id, [message])
    await session.commit()
    return row.id


async def action(
    session: AsyncSession,
    owner: AccountTable,
    sources: list[NormalizedMessage],
    *,
    status: str = "open",
    deleted: bool = False,
    minutes: int = 0,
    **fields: Any,
) -> str:
    """An action whose sources are these cached messages of ``owner``."""
    values: dict[str, Any] = {"deadline_precision": "none", **fields}
    row = await ActionRepository(session).add_action(
        ActionTable(
            public_id=str(uuid.uuid4()),
            title="Send the deck",
            ownership="waiting_for",
            status=status,
            notes="Ask Sam first.",
            created_at_utc=SOURCE_AT + timedelta(minutes=minutes),
            updated_at_utc=SOURCE_AT,
            completed_at_utc=SOURCE_AT if status == "completed" else None,
            deleted_at_utc=SOURCE_AT if deleted else None,
            revision=1,
            **values,
        )
    )
    repository = ActionRepository(session)
    repository.add_step(row.id, 0, "Collect the figures", None)
    for source in sources:
        message = await MessageRepository(session).get_by_provider_message_id(
            owner.id, source.provider_message_id
        )
        assert message is not None
        account_row = await session.get(AccountTable, message.account_id)
        assert account_row is not None
        await repository.add_source(row.id, message, account_row)
    await session.commit()
    return row.public_id


def signal(kind: FollowUpKind, **deadline: Any) -> MessageAnalysis:
    """An analysis with a follow-up signal; a new deadline defaults to next Monday."""
    values: dict[str, Any] = {
        "deadline_text": None,
        "deadline_precision": DeadlinePrecision.NONE,
        "deadline_date": None,
        "deadline_at_utc": None,
        "deadline_timezone": None,
    }
    if kind is FollowUpKind.NEW_DEADLINE:
        values.update(
            deadline_text="next Monday",
            deadline_precision=DeadlinePrecision.DATE,
            deadline_date=date(2026, 10, 5),
            deadline_timezone=ZONE,
        )
    values.update(deadline)
    return make_analysis(
        category="information",
        action_required=False,
        action_text=None,
        follow_up=kind,
        follow_up_evidence=MOVE,
        **values,
    )


def planned(
    message: NormalizedMessage, analysis: MessageAnalysis | None, row_id: int | None = None
) -> PlannedMessage:
    return PlannedMessage(
        ranked=RankedMessage(message=message, score=20),
        message_row_id=row_id,
        request=None,
        input_hash=None,
        outcome=AnalysisOutcome.ANALYZED,
        analysis=analysis,
    )


async def rows(session: AsyncSession) -> list[tuple[int, str, str]]:
    result = await session.execute(
        select(ActionProposalTable.action_id, ActionProposalTable.kind, ActionProposalTable.state)
        .order_by(ActionProposalTable.id)
        .execution_options(populate_existing=True)
    )
    return list(result.tuples())


async def count(session: AsyncSession) -> int:
    return (await session.scalar(select(func.count()).select_from(ActionProposalTable))) or 0


async def scene(
    session: AsyncSession, service: ProposalService, kind: FollowUpKind, **fields: Any
) -> tuple[AccountTable, str, NormalizedMessage]:
    """One open action from a Monday email, and a Wednesday reply in its thread proposing
    ``kind``."""
    owner = await account(session)
    source = email("source", at=SOURCE_AT)
    await cache(session, owner, source)
    public_id = await action(session, owner, [source], **{**FRIDAY, **fields})
    reply = email("reply")
    row_id = await cache(session, owner, reply)
    assert await service.derive(owner, [planned(reply, signal(kind), row_id)], ZONE) == 1
    return owner, public_id, reply


async def only(service: ProposalService) -> int:
    (proposal,) = await service.pending()
    return proposal.id


# Deriving


async def test_a_new_deadline_is_proposed_with_its_target_and_the_email_s_snapshot(
    session: AsyncSession, service: ProposalService
) -> None:
    _, public_id, _ = await scene(session, service, FollowUpKind.NEW_DEADLINE)
    before = await ActionService(session).get(public_id)

    (proposal,) = await service.pending()

    assert (proposal.action_public_id, proposal.action_title) == (public_id, "Send the deck")
    assert (proposal.kind, proposal.state) == (FollowUpKind.NEW_DEADLINE, ProposalState.PENDING)
    assert (proposal.deadline_precision, proposal.deadline_date, proposal.deadline_text) == (
        DeadlinePrecision.DATE,
        date(2026, 10, 5),
        "next Monday",
    )
    # One working day before Monday.
    assert (proposal.suggested_target_date, proposal.target_reason) == (
        date(2026, 10, 2),
        TargetReason.WORKING_DAY_BEFORE,
    )
    assert (proposal.evidence, proposal.provider_message_id, proposal.subject) == (
        MOVE,
        "reply",
        "Re: reply",
    )
    assert (proposal.sender_address, proposal.received_at_utc) == ("sam@example.com", REPLY_AT)
    assert proposal.created_at_utc == NOW
    # Deriving never changes the action.
    after = await ActionService(session).get(public_id)
    assert (after.revision, after.updated_at_utc, after.target_date) == (
        before.revision,
        before.updated_at_utc,
        before.target_date,
    )
    assert [item.id for item in after.proposals] == [proposal.id]


async def test_only_live_open_actions_whose_thread_the_email_continues_get_proposals(
    session: AsyncSession, service: ProposalService
) -> None:
    owner = await account(session)
    other = await account(session, "gmail-2")
    source = email("source", at=SOURCE_AT)
    await cache(session, owner, source)
    await cache(session, other, email("theirs", at=SOURCE_AT, owner="gmail-2"))
    await cache(session, owner, email("elsewhere", thread="budget", at=SOURCE_AT))
    await cache(session, owner, email("later", at=REPLY_AT + timedelta(hours=1)))
    reply = email("reply")
    row_id = await cache(session, owner, reply)
    kept = await action(session, owner, [source])
    await action(session, owner, [source], status="completed")
    await action(session, owner, [source], deleted=True)
    await action(session, owner, [email("elsewhere", thread="budget")])
    await action(
        session, other, [email("theirs", owner="gmail-2")]
    )  # Same thread ID, another account.
    await action(session, owner, [email("later"), source])  # Its latest source is newer.
    await action(session, owner, [source, reply])  # The email already belongs to it.

    created = await service.derive(
        owner, [planned(reply, signal(FollowUpKind.CANCELLED), row_id)], ZONE
    )

    assert created == 1
    assert [proposal.action_public_id for proposal in await service.pending()] == [kept]


SOON = {"deadline_text": "soon", "deadline_precision": "unresolved"}


@pytest.mark.parametrize(
    ("current", "proposed"),
    [
        (FRIDAY, {"deadline_text": "Friday", "deadline_date": date(2026, 10, 2)}),
        (
            SOON,
            {
                "deadline_text": "soon",
                "deadline_precision": DeadlinePrecision.UNRESOLVED,
                "deadline_date": None,
                "deadline_timezone": None,
            },
        ),
    ],
    ids=["same-day", "same-words"],
)
async def test_a_new_deadline_equal_to_the_action_s_proposes_nothing(
    session: AsyncSession,
    service: ProposalService,
    current: dict[str, Any],
    proposed: dict[str, Any],
) -> None:
    owner = await account(session)
    source = email("source", at=SOURCE_AT)
    await cache(session, owner, source)
    await action(session, owner, [source], **current)
    reply = email("reply")
    row_id = await cache(session, owner, reply)
    same = signal(FollowUpKind.NEW_DEADLINE, **proposed)

    assert await service.derive(owner, [planned(reply, same, row_id)], ZONE) == 0
    assert await count(session) == 0
    if current is SOON:  # Other words are a change.
        moved = signal(FollowUpKind.NEW_DEADLINE, **{**proposed, "deadline_text": "later"})
        assert await service.derive(owner, [planned(reply, moved, row_id)], ZONE) == 1


async def test_at_most_three_actions_most_urgent_first(
    session: AsyncSession, service: ProposalService
) -> None:
    owner = await account(session)
    source = email("source", at=SOURCE_AT)
    await cache(session, owner, source)
    undated = await action(session, owner, [source])
    late = await action(session, owner, [source], target_date=date(2026, 10, 9))
    soon = await action(session, owner, [source], target_date=date(2026, 10, 1))
    later = await action(session, owner, [source], target_date=date(2026, 10, 20))
    reply = email("reply")
    row_id = await cache(session, owner, reply)

    created = await service.derive(
        owner, [planned(reply, signal(FollowUpKind.DELIVERED), row_id)], ZONE
    )

    assert created == MAX_PROPOSALS_PER_EMAIL == 3
    proposed = {proposal.action_public_id for proposal in await service.pending()}
    assert proposed == {soon, late, later}
    assert undated not in proposed


@pytest.mark.parametrize(
    "aliases",
    [
        ["gmail-1@example.com", "alias@example.org"],
        ["Alias@Example.org"],  # An alias list that lacks the primary address.
    ],
)
async def test_the_owner_s_own_messages_propose_nothing(
    session: AsyncSession, service: ProposalService, aliases: list[str]
) -> None:
    owner = await account(session)
    owner.account_addresses = aliases
    await session.commit()
    source = email("source", at=SOURCE_AT)
    await cache(session, owner, source)
    await action(session, owner, [source])
    reply = email("reply")

    def from_address(key: str, address: str) -> NormalizedMessage:
        sender = EmailContact(name="Me", address=address)
        return reply.model_copy(update={"provider_message_id": key, "sender": sender})

    own = [
        reply.model_copy(update={"provider_message_id": "sent", "is_sent": True}),
        from_address("primary", " GMAIL-1@Example.COM "),
        from_address("alias", "alias@example.org"),
    ]
    signals = FollowUpKind.CANCELLED

    assert await service.derive(owner, [planned(m, signal(signals)) for m in own], ZONE) == 0
    assert await count(session) == 0
    # A message from anyone else still proposes, so the rule is about the sender only.
    assert await service.derive(owner, [planned(reply, signal(signals))], ZONE) == 1


async def test_the_cap_is_exact_across_apply_dismiss_and_closing(
    session: AsyncSession, service: ProposalService
) -> None:
    owner = await account(session)
    source = email("source", at=SOURCE_AT)
    await cache(session, owner, source)
    # Four open actions in one thread, most urgent first.
    first, second, third, fourth = [
        await action(session, owner, [source], target_date=date(2026, 10, day))
        for day in (1, 2, 3, 4)
    ]
    reply = email("reply")
    row_id = await cache(session, owner, reply)
    items = [planned(reply, signal(FollowUpKind.NEW_DEADLINE), row_id)]

    assert await service.derive(owner, items, ZONE) == 3
    by_action = {proposal.action_public_id: proposal.id for proposal in await service.pending()}
    assert set(by_action) == {first, second, third}

    # A new deadline leaves the action open and makes the email one of its sources, so the
    # first action stops being a candidate; the fourth must not take its place.
    await service.apply(by_action[first], 1)
    assert await service.derive(owner, items, ZONE) == 0
    # Nor does dismissing one, or completing another, free a slot.
    await service.dismiss(by_action[second])
    await ActionService(session).complete(third, 1)
    assert await service.derive(owner, items, ZONE) == 0

    assert len(await rows(session)) == MAX_PROPOSALS_PER_EMAIL
    assert fourth not in {proposal.action_public_id for proposal in await service.pending()}
    # A later email has its own three slots: the open actions it continues, here three (the
    # third was completed).
    other = email("other", at=REPLY_AT + timedelta(hours=1))
    other_id = await cache(session, owner, other)
    later = signal(
        FollowUpKind.NEW_DEADLINE, deadline_text="the 12th", deadline_date=date(2026, 10, 12)
    )
    assert await service.derive(owner, [planned(other, later, other_id)], ZONE) == 3


async def test_a_same_deadline_action_uses_no_slot(
    session: AsyncSession, service: ProposalService
) -> None:
    owner = await account(session)
    source = email("source", at=SOURCE_AT)
    await cache(session, owner, source)
    monday: dict[str, Any] = {
        "deadline_text": "next Monday",
        "deadline_precision": "date",
        "deadline_date": date(2026, 10, 5),
        "deadline_timezone": ZONE,
    }
    await action(session, owner, [source], target_date=date(2026, 10, 1), **monday)
    others = [
        await action(session, owner, [source], target_date=date(2026, 10, day)) for day in (2, 3, 4)
    ]
    reply = email("reply")
    row_id = await cache(session, owner, reply)

    created = await service.derive(
        owner, [planned(reply, signal(FollowUpKind.NEW_DEADLINE), row_id)], ZONE
    )

    assert created == MAX_PROPOSALS_PER_EMAIL
    assert {proposal.action_public_id for proposal in await service.pending()} == set(others)


async def test_deriving_again_changes_nothing_and_dismissed_proposals_never_return(
    session: AsyncSession, service: ProposalService
) -> None:
    owner, _, reply = await scene(session, service, FollowUpKind.CANCELLED)
    again = [planned(reply, signal(FollowUpKind.CANCELLED))]

    assert await service.derive(owner, again, ZONE) == 0
    await service.dismiss(await only(service))
    assert await service.derive(owner, again, ZONE) == 0
    assert [state for _, _, state in await rows(session)] == ["dismissed"]


async def test_a_newer_signal_replaces_a_pending_proposal_of_another_kind_only(
    session: AsyncSession, service: ProposalService
) -> None:
    owner, _, reply = await scene(session, service, FollowUpKind.CANCELLED)

    assert await service.derive(owner, [planned(reply, signal(FollowUpKind.DELIVERED))], ZONE) == 1
    assert [(kind, state) for _, kind, state in await rows(session)] == [("delivered", "pending")]

    await service.dismiss(await only(service))
    assert await service.derive(owner, [planned(reply, signal(FollowUpKind.CANCELLED))], ZONE) == 1
    # The dismissed one is the owner's decision and stays.
    assert [(kind, state) for _, kind, state in await rows(session)] == [
        ("delivered", "dismissed"),
        ("cancelled", "pending"),
    ]


async def test_messages_without_a_signal_or_a_thread_propose_nothing(
    session: AsyncSession, service: ProposalService
) -> None:
    owner = await account(session)
    source = email("source", at=SOURCE_AT)
    await cache(session, owner, source)
    await action(session, owner, [source])
    quiet = make_analysis()

    items = [
        planned(email("quiet"), quiet),
        planned(email("unanalyzed"), None),
        planned(email("threadless", thread=None), signal(FollowUpKind.CANCELLED)),
    ]

    assert await service.derive(owner, items, ZONE) == 0
    assert await service.derive(owner, [], ZONE) == 0


async def test_a_cached_analysis_proposes_like_a_new_one(
    session: AsyncSession, service: ProposalService
) -> None:
    owner = await account(session)
    source = email("source", at=SOURCE_AT)
    await cache(session, owner, source)
    public_id = await action(session, owner, [source])
    reply = email("reply")
    row_id = await cache(session, owner, reply)
    stored = await AnalysisRepository(session).upsert_analysis(
        message_id=row_id,
        input_hash="hash",
        provider="groq",
        model="model",
        prompt_version="prompt",
        schema_version="7",
        analysis=signal(FollowUpKind.DELIVERED),
    )
    await session.commit()
    cached = AnalysisRepository.to_domain(stored, message_key="key")
    item = planned(reply, cached, row_id)
    item.outcome = AnalysisOutcome.REUSED

    assert (cached.follow_up, cached.follow_up_evidence) == (FollowUpKind.DELIVERED, MOVE)
    assert await service.derive(owner, [item], ZONE) == 1
    assert (await service.pending())[0].action_public_id == public_id


# Applying


async def test_applying_a_new_deadline_moves_a_target_the_owner_never_changed(
    session: AsyncSession, service: ProposalService, clock: Clock
) -> None:
    _, public_id, _ = await scene(session, service, FollowUpKind.NEW_DEADLINE)
    proposal_id = await only(service)
    clock.now = NOW + timedelta(hours=1)

    applied = await service.apply(proposal_id, 1)

    assert (applied.revision, applied.updated_at_utc) == (2, clock.now)
    assert (await service.get(proposal_id)).action_revision == 2  # Loaded after the change.
    assert (applied.deadline_text, applied.deadline_date) == ("next Monday", date(2026, 10, 5))
    assert (applied.suggested_target_date, applied.target_date) == (
        date(2026, 10, 2),
        date(2026, 10, 2),
    )
    assert applied.status is ActionStatus.OPEN
    assert [(source.provider_message_id, source.available) for source in applied.sources] == [
        ("source", True),
        ("reply", True),
    ]
    assert applied.proposals == ()
    unchanged(applied)
    decided = await service.get(proposal_id)
    assert decided.state is ProposalState.APPLIED


def unchanged(action: Action) -> None:
    """Applying never touches the owner's title, notes, steps or ownership."""
    assert (action.title, action.notes, action.ownership) == (
        "Send the deck",
        "Ask Sam first.",
        ActionOwnership.WAITING_FOR,
    )
    assert [step.text for step in action.steps] == ["Collect the figures"]


async def test_applying_a_new_deadline_keeps_a_target_the_owner_set(
    session: AsyncSession, service: ProposalService
) -> None:
    _, _, _ = await scene(
        session, service, FollowUpKind.NEW_DEADLINE, target_date=date(2026, 9, 30)
    )

    applied = await service.apply(await only(service), 1)

    assert applied.suggested_target_date == date(2026, 10, 2)
    assert applied.target_date == date(2026, 9, 30)


@pytest.mark.parametrize("kind", [FollowUpKind.CANCELLED, FollowUpKind.DELIVERED])
async def test_a_cancellation_or_delivery_completes_the_action(
    session: AsyncSession, service: ProposalService, kind: FollowUpKind
) -> None:
    await scene(session, service, kind)

    applied = await service.apply(await only(service), 1)

    assert (applied.status, applied.completed_at_utc, applied.revision) == (
        ActionStatus.COMPLETED,
        NOW,
        2,
    )
    assert applied.deadline_date == date(2026, 10, 2)  # Its deadline is left as it was.
    unchanged(applied)


async def test_an_email_gone_from_local_mail_becomes_a_source_from_its_snapshot(
    session: AsyncSession, service: ProposalService
) -> None:
    await scene(session, service, FollowUpKind.CANCELLED)
    await session.execute(delete(MessageTable).where(MessageTable.provider_message_id == "reply"))
    await session.commit()

    applied = await service.apply(await only(service), 1)

    source = applied.sources[-1]
    assert (source.provider_message_id, source.available, source.subject) == (
        "reply",
        False,
        "Re: reply",
    )
    snapshot = await session.scalar(
        select(ActionSourceTable.provider_thread_id).where(
            ActionSourceTable.provider_message_id == "reply"
        )
    )
    assert snapshot == "deck"


async def test_applying_is_refused_for_a_stale_or_decided_proposal_or_a_closed_action(
    session: AsyncSession, service: ProposalService
) -> None:
    _, public_id, _ = await scene(session, service, FollowUpKind.CANCELLED)
    proposal_id = await only(service)

    with pytest.raises(ActionConflictError, match="changed since"):
        await service.apply(proposal_id, 7)
    await ActionService(session).complete(public_id, 1)
    with pytest.raises(ActionConflictError, match="no longer open"):
        await service.apply(proposal_id, 2)
    await ActionService(session).reopen(public_id, 2)
    await service.dismiss(proposal_id)
    with pytest.raises(ActionConflictError, match="already applied or dismissed"):
        await service.apply(proposal_id, 3)
    with pytest.raises(ProposalNotFoundError):
        await service.apply(999, 3)
    assert (await ActionService(session).get(public_id)).revision == 3


# Undoing, dismissing and restoring


async def test_undo_restores_the_action_and_the_proposal_in_one_revision(
    session: AsyncSession, service: ProposalService
) -> None:
    _, public_id, _ = await scene(session, service, FollowUpKind.NEW_DEADLINE)
    proposal_id = await only(service)
    before = await ActionService(session).get(public_id)
    await service.apply(proposal_id, 1)

    undone = await service.undo_apply(proposal_id, 2)

    assert undone.revision == 3
    fields = ("deadline_text", "deadline_date", "suggested_target_date", "target_date", "status")
    assert [getattr(undone, name) for name in fields] == [getattr(before, name) for name in fields]
    assert undone.target_reason is TargetReason.WORKING_DAY_BEFORE
    assert [source.provider_message_id for source in undone.sources] == ["source"]
    assert [proposal.id for proposal in undone.proposals] == [proposal_id]


async def test_undo_reopens_a_completed_action(
    session: AsyncSession, service: ProposalService
) -> None:
    await scene(session, service, FollowUpKind.DELIVERED)
    proposal_id = await only(service)
    await service.apply(proposal_id, 1)

    undone = await service.undo_apply(proposal_id, 2)

    assert (undone.status, undone.completed_at_utc) == (ActionStatus.OPEN, None)


async def test_undo_is_refused_once_the_action_changed(
    session: AsyncSession, service: ProposalService
) -> None:
    _, public_id, _ = await scene(session, service, FollowUpKind.NEW_DEADLINE)
    proposal_id = await only(service)
    applied = await service.apply(proposal_id, 1)
    await ActionService(session).save(
        public_id,
        2,
        ActionEdit(
            title="Send the deck",
            ownership=ActionOwnership.WAITING_FOR,
            effort=None,
            target_date=applied.target_date,
            notes="Moved.",
        ),
        [StepEdit(step_id=applied.steps[0].step_id, text="Collect the figures", done=True)],
    )

    for revision in (2, 3):
        with pytest.raises(ActionConflictError, match="unchanged update"):
            await service.undo_apply(proposal_id, revision)
    with pytest.raises(ProposalNotFoundError):
        await service.undo_apply(999, 3)


async def test_undo_keeps_a_source_the_apply_did_not_add(
    session: AsyncSession, service: ProposalService
) -> None:
    owner, public_id, reply = await scene(session, service, FollowUpKind.CANCELLED)
    proposal_id = await only(service)
    # The owner linked the email meanwhile, so applying adds no source.
    message = await MessageRepository(session).get_by_provider_message_id(owner.id, "reply")
    assert message is not None and reply.provider_message_id == "reply"
    row = await ActionRepository(session).get_action(public_id)
    assert row is not None
    await ActionRepository(session).add_source(row.id, message, owner)
    await session.commit()
    await service.apply(proposal_id, 1)

    undone = await service.undo_apply(proposal_id, 2)

    assert [source.provider_message_id for source in undone.sources] == ["source", "reply"]


async def test_dismiss_and_restore_change_no_action_revision(
    session: AsyncSession, service: ProposalService
) -> None:
    _, public_id, _ = await scene(session, service, FollowUpKind.CANCELLED)
    proposal_id = await only(service)

    await service.dismiss(proposal_id)
    await service.dismiss(proposal_id)  # Already dismissed: nothing changes.
    assert (await service.get(proposal_id)).state is ProposalState.DISMISSED
    assert await service.pending() == ()
    await service.restore(proposal_id)
    await service.restore(proposal_id)
    assert (await service.get(proposal_id)).state is ProposalState.PENDING
    assert (await ActionService(session).get(public_id)).revision == 1

    await service.apply(proposal_id, 1)
    for change in (service.dismiss, service.restore):
        with pytest.raises(ActionConflictError, match="undo it instead"):
            await change(proposal_id)
    with pytest.raises(ProposalNotFoundError):
        await service.get(999)


# Listing


async def test_pending_proposals_are_listed_by_message_for_open_actions_only(
    session: AsyncSession, service: ProposalService
) -> None:
    _, public_id, _ = await scene(session, service, FollowUpKind.CANCELLED)

    found = await service.pending_for_messages("gmail-1@example.com", ["reply", "other"])

    assert list(found) == ["reply"]
    assert [proposal.action_public_id for proposal in found["reply"]] == [public_id]
    # Each carries the action's revision as loaded: what applying it will need.
    assert [proposal.action_revision for proposal in found["reply"]] == [1]
    assert [proposal.action_revision for proposal in await service.pending()] == [1]
    assert await service.pending_for_messages("gmail-2@example.com", ["reply"]) == {}
    await ActionService(session).complete(public_id, 1)
    assert await service.pending_for_messages("gmail-1@example.com", ["reply"]) == {}
    assert await service.pending() == ()
    with pytest.raises(ValueError):
        await service.pending(0)
