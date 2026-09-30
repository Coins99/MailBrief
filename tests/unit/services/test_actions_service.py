"""Accepted actions: acceptance, decisions, edits, steps, lifecycle, lists and durability."""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import (
    Action,
    ActionEdit,
    ActionFilter,
    ActionStatus,
    StepEdit,
    SuggestionState,
)
from mailbrief.domain.analysis import (
    ActionEffort,
    ActionOwnership,
    ActionSuggestion,
    DeadlinePrecision,
    TargetReason,
)
from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.services.actions import (
    ActionConflictError,
    ActionNotFoundError,
    ActionService,
    SuggestionNotFoundError,
)
from mailbrief.services.analysis import suggestion_fingerprint
from mailbrief.storage.actions import ActionRepository, suggestion_views
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, AnalysisRepository, MessageRepository
from mailbrief.storage.tables import (
    AccountTable,
    ActionSourceTable,
    ActionStepTable,
    ActionSuggestionTable,
    ActionTable,
    MessageTable,
    SuggestionDecisionTable,
)
from tests.factories import make_analysis, make_message, make_suggestion

START = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)  # A Monday.
TORONTO = ZoneInfo("America/Toronto")


class Clock:
    """A clock the test moves forward."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class Ids:
    """Predictable public IDs."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> str:
        self.count += 1
        return f"00000000-0000-4000-8000-{self.count:012d}"


def suggestion(position: int, title: str, **overrides: Any) -> ActionSuggestion:
    return make_suggestion(
        position=position, title=title, fingerprint=suggestion_fingerprint(title), **overrides
    )


DATED = suggestion(
    0,
    "Send the Q3 deck",
    effort=ActionEffort.HOURS,
    deadline_text="by Friday",
    deadline_precision=DeadlinePrecision.DATE,
    deadline_date=date(2026, 10, 2),
    deadline_timezone="America/Toronto",
    suggested_target_date=date(2026, 10, 1),
    target_reason=TargetReason.WORKING_DAY_BEFORE,
    steps=("Collect the figures", "Draft the slides"),
    evidence="Please send the Q3 deck by Friday",
)
PLAIN = suggestion(1, "Book the room", steps=(), evidence=None)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.from_path(tmp_path / "actions.sqlite3")
    await database.create_schema_for_tests()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session() as session:
        yield session


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(session: AsyncSession, clock: Clock) -> ActionService:
    return ActionService(session, clock=clock, id_factory=Ids())


class Seeded:
    """One message with an analysis and its stored suggestions."""

    def __init__(self, message_id: int, analysis_id: int, suggestion_ids: list[int]) -> None:
        self.message_id = message_id
        self.analysis_id = analysis_id
        self.suggestion_ids = suggestion_ids


async def seed(
    session: AsyncSession,
    suggestions: tuple[ActionSuggestion, ...] = (DATED, PLAIN),
    *,
    message: str = "msg-1",
    prompt_version: str = "prompt-1",
    thread: str = "conversation-1",
) -> Seeded:
    account = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.GMAIL, provider_account_id="gmail-1", email_address="me@x.com"
        )
    )
    (row,) = await MessageRepository(session).upsert_messages(
        account.id,
        [
            make_message(
                provider_message_id=message,
                conversation_id=thread,
                subject=f"Subject of {message}",
                web_link=f"https://mail.google.com/mail/u/?authuser=me%40x.com#all/{message}",
            )
        ],
    )
    analysis = await AnalysisRepository(session).upsert_analysis(
        message_id=row.id,
        input_hash=f"hash-{message}",
        provider="groq",
        model="model-1",
        prompt_version=prompt_version,
        schema_version="6",
        analysis=make_analysis(suggestions=suggestions),
    )
    await session.commit()
    ids = list(
        await session.scalars(
            select(ActionSuggestionTable.id)
            .where(ActionSuggestionTable.analysis_id == analysis.id)
            .order_by(ActionSuggestionTable.position)
        )
    )
    return Seeded(row.id, analysis.id, ids)


async def reanalyze(
    session: AsyncSession, seeded: Seeded, suggestions: tuple[ActionSuggestion, ...], version: str
) -> Seeded:
    """A new analysis of the same message under another prompt version."""
    analysis = await AnalysisRepository(session).upsert_analysis(
        message_id=seeded.message_id,
        input_hash=f"hash-{seeded.message_id}",
        provider="groq",
        model="model-1",
        prompt_version=version,
        schema_version="6",
        analysis=make_analysis(suggestions=suggestions),
    )
    await session.commit()
    ids = list(
        await session.scalars(
            select(ActionSuggestionTable.id)
            .where(ActionSuggestionTable.analysis_id == analysis.id)
            .order_by(ActionSuggestionTable.position)
        )
    )
    return Seeded(seeded.message_id, analysis.id, ids)


async def states(session: AsyncSession, seeded: Seeded) -> list[tuple[SuggestionState, str | None]]:
    views = await suggestion_views(session, [(seeded.message_id, seeded.analysis_id)])
    return [(view.state, view.action_public_id) for view in views[seeded.message_id]]


async def count(session: AsyncSession, table: type[Any]) -> int:
    return (await session.scalar(select(func.count()).select_from(table))) or 0


async def counts(session: AsyncSession) -> tuple[int, int, int, int]:
    return (
        await count(session, ActionTable),
        await count(session, ActionStepTable),
        await count(session, ActionSourceTable),
        await count(session, SuggestionDecisionTable),
    )


# Accepting


async def test_accept_copies_the_suggestion_its_steps_and_a_source_snapshot(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)

    action = await service.accept(seeded.suggestion_ids[0])

    assert action.public_id == "00000000-0000-4000-8000-000000000001"
    assert (action.title, action.ownership, action.status) == (
        "Send the Q3 deck",
        ActionOwnership.MINE,
        ActionStatus.OPEN,
    )
    assert action.effort is ActionEffort.HOURS
    assert (action.deadline_precision, action.deadline_date, action.deadline_text) == (
        DeadlinePrecision.DATE,
        date(2026, 10, 2),
        "by Friday",
    )
    assert action.suggested_target_date == action.target_date == date(2026, 10, 1)
    assert action.target_reason is TargetReason.WORKING_DAY_BEFORE
    assert action.evidence == "Please send the Q3 deck by Friday"
    assert (action.notes, action.revision) == ("", 1)
    assert action.created_at_utc == action.updated_at_utc == START
    assert [(step.text, step.done, step.position) for step in action.steps] == [
        ("Collect the figures", False, 0),
        ("Draft the slides", False, 1),
    ]
    (source,) = action.sources
    assert (source.provider_message_id, source.subject, source.available, source.in_inbox) == (
        "msg-1",
        "Subject of msg-1",
        True,
        True,
    )
    assert str(source.web_link).startswith("https://mail.google.com/")
    assert await states(session, seeded) == [
        (SuggestionState.ACCEPTED, action.public_id),
        (SuggestionState.PENDING, None),
    ]


async def test_accepting_twice_returns_the_same_action_and_adds_no_rows(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    first = await service.accept(seeded.suggestion_ids[0])
    before = await counts(session)

    second = await service.accept(seeded.suggestion_ids[0])

    assert second == first
    assert await counts(session) == before == (1, 2, 1, 1)


async def test_accepting_after_a_dismissal_reuses_the_decision_row(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    await service.dismiss(seeded.suggestion_ids[0])

    action = await service.accept(seeded.suggestion_ids[0])

    assert await count(session, SuggestionDecisionTable) == 1
    assert (await states(session, seeded))[0] == (SuggestionState.ACCEPTED, action.public_id)


async def test_an_accepted_decision_whose_action_is_gone_accepts_again_in_the_same_row(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    first = await service.accept(seeded.suggestion_ids[0])
    await session.execute(delete(ActionTable))  # Outright; the decision's action_id is SET NULL.
    await session.commit()
    session.expunge_all()
    assert (await states(session, seeded))[0] == (SuggestionState.PENDING, None)

    second = await service.accept(seeded.suggestion_ids[0])

    assert second.public_id != first.public_id
    assert await count(session, SuggestionDecisionTable) == 1
    assert (await states(session, seeded))[0] == (SuggestionState.ACCEPTED, second.public_id)


async def test_accepting_again_after_deleting_the_action_restores_that_same_action(
    session: AsyncSession, service: ActionService, clock: Clock
) -> None:
    seeded = await seed(session)
    first = await service.accept(seeded.suggestion_ids[0])
    await service.delete(first.public_id, 1)
    assert await session.scalar(select(ActionTable.revision)) == 2
    before = await counts(session)
    clock.advance(minutes=1)

    restored = await service.accept(seeded.suggestion_ids[0])

    assert restored == first.model_copy(
        update={"revision": 3, "updated_at_utc": START + timedelta(minutes=1)}
    )
    assert await counts(session) == before == (1, 2, 1, 1)
    assert (await states(session, seeded))[0] == (SuggestionState.ACCEPTED, first.public_id)


async def test_unknown_or_unreadable_suggestions_are_not_found(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    with pytest.raises(SuggestionNotFoundError):
        await service.accept(999_999)
    await session.execute(
        update(ActionSuggestionTable)
        .where(ActionSuggestionTable.id == seeded.suggestion_ids[0])
        .values(steps_json={"not": "a list"})
    )
    await session.commit()
    session.expunge_all()

    with pytest.raises(SuggestionNotFoundError) as caught:
        await service.accept(seeded.suggestion_ids[0])

    assert "Q3" not in str(caught.value)
    assert await count(session, ActionTable) == 0


# Dismissing and restoring


async def test_a_dismissal_survives_a_reanalysis_that_rewrites_the_title_case(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    await service.dismiss(seeded.suggestion_ids[0])

    again = await reanalyze(session, seeded, (suggestion(0, "SEND the Q3 deck!"),), "prompt-2")

    assert again.analysis_id != seeded.analysis_id
    assert await states(session, again) == [(SuggestionState.DISMISSED, None)]


async def test_a_reworded_title_is_offered_again(
    session: AsyncSession, service: ActionService
) -> None:
    """The documented limit: decisions match titles, so a new wording is a new suggestion."""
    seeded = await seed(session)
    await service.dismiss(seeded.suggestion_ids[0])

    again = await reanalyze(session, seeded, (suggestion(0, "Share the Q3 slides"),), "prompt-2")

    assert await states(session, again) == [(SuggestionState.PENDING, None)]


async def test_dismissing_twice_keeps_the_first_decision_time(
    session: AsyncSession, service: ActionService, clock: Clock
) -> None:
    seeded = await seed(session)
    await service.dismiss(seeded.suggestion_ids[1])
    clock.advance(hours=1)

    await service.dismiss(seeded.suggestion_ids[1])

    decided = await session.scalar(select(SuggestionDecisionTable.decided_at_utc))
    assert decided == START


async def test_an_accepted_suggestion_cannot_be_dismissed(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])

    with pytest.raises(ActionConflictError):
        await service.dismiss(seeded.suggestion_ids[0])

    assert (await states(session, seeded))[0] == (SuggestionState.ACCEPTED, action.public_id)


async def test_restore_suggestion_undoes_a_dismissal(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    await service.dismiss(seeded.suggestion_ids[1])

    await service.restore_suggestion(seeded.suggestion_ids[1])
    await service.restore_suggestion(seeded.suggestion_ids[1])  # Nothing left to undo.

    assert (await states(session, seeded))[1] == (SuggestionState.PENDING, None)
    assert await count(session, SuggestionDecisionTable) == 0


async def test_restore_suggestion_brings_back_a_deleted_action_and_leaves_a_live_one(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])
    await service.restore_suggestion(seeded.suggestion_ids[0])  # Live: no change.
    assert (await service.get(action.public_id)).revision == 1
    await service.delete(action.public_id, 1)
    assert (await states(session, seeded))[0] == (SuggestionState.DISMISSED, None)

    await service.restore_suggestion(seeded.suggestion_ids[0])

    restored = await service.get(action.public_id)
    assert restored.revision == 3
    assert (await states(session, seeded))[0] == (SuggestionState.ACCEPTED, action.public_id)


async def test_dismissing_after_deleting_the_action_keeps_the_link_for_restore(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])
    await service.delete(action.public_id, 1)

    await service.dismiss(seeded.suggestion_ids[0])  # Already shown as dismissed.
    await service.restore(action.public_id)

    assert (await states(session, seeded))[0] == (SuggestionState.ACCEPTED, action.public_id)
    assert await count(session, ActionTable) == 1


async def test_restoring_a_suggestion_whose_action_is_gone_makes_it_pending(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    await service.accept(seeded.suggestion_ids[0])
    await session.execute(delete(ActionTable))
    await session.commit()
    session.expunge_all()

    await service.restore_suggestion(seeded.suggestion_ids[0])

    assert await count(session, SuggestionDecisionTable) == 0


async def test_dismissing_a_suggestion_whose_action_is_gone_records_it_in_the_same_row(
    session: AsyncSession, service: ActionService, clock: Clock
) -> None:
    seeded = await seed(session)
    await service.accept(seeded.suggestion_ids[0])
    await session.execute(delete(ActionTable))  # Outright; the decision's action_id is SET NULL.
    await session.commit()
    session.expunge_all()
    decision_id = await session.scalar(select(SuggestionDecisionTable.id))
    clock.advance(hours=1)

    await service.dismiss(seeded.suggestion_ids[0])

    stored = await session.execute(
        select(
            SuggestionDecisionTable.id,
            SuggestionDecisionTable.decision,
            SuggestionDecisionTable.action_id,
            SuggestionDecisionTable.decided_at_utc,
        )
    )
    assert stored.tuples().one() == (decision_id, "dismissed", None, START + timedelta(hours=1))
    assert (await states(session, seeded))[0] == (SuggestionState.DISMISSED, None)


async def test_a_suggestion_dismissed_then_restored_can_be_accepted(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    await service.dismiss(seeded.suggestion_ids[0])
    await service.restore_suggestion(seeded.suggestion_ids[0])

    action = await service.accept(seeded.suggestion_ids[0])

    assert action.title == "Send the Q3 deck"
    assert await count(session, SuggestionDecisionTable) == 1
    assert (await states(session, seeded))[0] == (SuggestionState.ACCEPTED, action.public_id)


# Edits never lost to regeneration


async def test_a_manual_edit_survives_reanalysis_with_different_suggestions(
    session: AsyncSession, service: ActionService, clock: Clock
) -> None:
    seeded = await seed(session)
    accepted = await service.accept(seeded.suggestion_ids[0])
    clock.advance(minutes=5)
    edited = await service.save(
        accepted.public_id,
        1,
        ActionEdit(
            title="Send the final Q3 deck",
            ownership=ActionOwnership.MINE,
            effort=ActionEffort.DAYS,
            target_date=date(2026, 9, 30),
            notes="Ask Sam for the churn slide.",
        ),
        [StepEdit(step_id=accepted.steps[1].step_id, text="Draft the slides", done=True)],
    )
    before = await counts(session)
    clock.advance(hours=1)

    await reanalyze(
        session,
        seeded,
        (suggestion(0, "Send the Q3 deck", steps=("Something else",)), suggestion(1, "New")),
        "prompt-2",
    )

    assert await service.get(accepted.public_id) == edited
    assert await counts(session) == before
    assert edited.revision == 2
    assert edited.updated_at_utc == START + timedelta(minutes=5)


# Revisions


@pytest.mark.parametrize(
    "change",
    [
        "save",
        "save_with_plan",
        "complete",
        "delete",
        "unaccept",
        "accept_into",
    ],
)
async def test_a_stale_revision_conflicts_and_changes_nothing(
    session: AsyncSession, service: ActionService, change: str
) -> None:
    seeded = await seed(session)
    first = await service.accept(seeded.suggestion_ids[0])
    await service.save(
        first.public_id,
        1,
        ActionEdit(
            title="Renamed", ownership=ActionOwnership.MINE, effort=None, target_date=None, notes=""
        ),
    )
    other = await seed(session, (suggestion(0, "Other"),), message="msg-2")
    stale = 1
    lost = ActionEdit(
        title="Lost", ownership=ActionOwnership.MINE, effort=None, target_date=None, notes=""
    )
    calls: dict[str, Any] = {
        "save": lambda: service.save(first.public_id, stale, lost),
        "save_with_plan": lambda: service.save(
            first.public_id,
            stale,
            lost,
            [StepEdit(step_id=first.steps[0].step_id, text="Collect the figures", done=True)],
        ),
        "complete": lambda: service.complete(first.public_id, stale),
        "delete": lambda: service.delete(first.public_id, stale),
        "unaccept": lambda: service.unaccept(first.public_id, stale),
        "accept_into": lambda: service.accept_into(other.suggestion_ids[0], first.public_id, stale),
    }
    before = await service.get(first.public_id)

    with pytest.raises(ActionConflictError) as caught:
        await calls[change]()

    assert "Renamed" not in str(caught.value)
    assert await service.get(first.public_id) == before


async def test_reopen_with_a_stale_revision_conflicts(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])
    await service.complete(action.public_id, 1)

    with pytest.raises(ActionConflictError):
        await service.reopen(action.public_id, 1)


async def test_a_second_save_from_the_same_revision_conflicts_and_keeps_the_first(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])

    def edit(title: str, notes: str) -> ActionEdit:
        return ActionEdit(
            title=title, ownership=ActionOwnership.MINE, effort=None, target_date=None, notes=notes
        )

    first = await service.save(action.public_id, 1, edit("From one screen", "First"))
    with pytest.raises(ActionConflictError):
        await service.save(action.public_id, 1, edit("From another screen", "Second"))

    assert await service.get(action.public_id) == first
    assert (first.title, first.notes, first.revision) == ("From one screen", "First", 2)


# Undoing an acceptance


async def test_unaccept_removes_an_unchanged_action_and_its_decision(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])

    await service.unaccept(action.public_id, 1)

    assert await counts(session) == (0, 0, 0, 0)
    assert (await states(session, seeded))[0] == (SuggestionState.PENDING, None)


async def test_unaccept_is_refused_once_the_action_changed_or_has_two_sources(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    edited = await service.accept(seeded.suggestion_ids[0])
    await service.save(
        edited.public_id,
        1,
        ActionEdit(
            title="Edited", ownership=ActionOwnership.MINE, effort=None, target_date=None, notes=""
        ),
    )
    with pytest.raises(ActionConflictError):
        await service.unaccept(edited.public_id, 2)

    linked = await service.accept(seeded.suggestion_ids[1])
    other = await seed(session, (suggestion(0, "Other"),), message="msg-2")
    await service.accept_into(other.suggestion_ids[0], linked.public_id, 1)
    with pytest.raises(ActionConflictError):
        await service.unaccept(linked.public_id, 2)

    assert await count(session, ActionTable) == 2


async def test_unaccept_is_refused_for_a_completed_action(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])
    await service.complete(action.public_id, 1)

    with pytest.raises(ActionConflictError):
        await service.unaccept(action.public_id, 2)


async def test_accepting_again_after_unaccept_makes_a_new_action_with_one_decision(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    first = await service.accept(seeded.suggestion_ids[0])
    await service.unaccept(first.public_id, 1)

    second = await service.accept(seeded.suggestion_ids[0])

    assert second.public_id != first.public_id
    assert await counts(session) == (1, 2, 1, 1)
    assert (await states(session, seeded))[0] == (SuggestionState.ACCEPTED, second.public_id)
    with pytest.raises(ActionNotFoundError):
        await service.get(first.public_id)


# Completing and reopening


async def test_complete_and_reopen_set_and_clear_the_time_and_leave_steps(
    session: AsyncSession, service: ActionService, clock: Clock
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])
    clock.advance(hours=2)

    completed = await service.complete(action.public_id, 1)
    with pytest.raises(ActionConflictError):
        await service.complete(action.public_id, 2)
    reopened = await service.reopen(action.public_id, 2)
    with pytest.raises(ActionConflictError):
        await service.reopen(action.public_id, 3)

    assert (completed.status, completed.completed_at_utc) == (
        ActionStatus.COMPLETED,
        START + timedelta(hours=2),
    )
    assert (reopened.status, reopened.completed_at_utc, reopened.revision) == (
        ActionStatus.OPEN,
        None,
        3,
    )
    assert completed.steps == reopened.steps == action.steps


# Steps


def plan(action: Action) -> ActionEdit:
    """The action's own fields as they are; the steps go to save() separately."""
    return ActionEdit(
        title=action.title,
        ownership=action.ownership,
        effort=action.effort,
        target_date=action.target_date,
        notes=action.notes,
    )


def steps(*items: tuple[int | None, str, bool]) -> list[StepEdit]:
    return [StepEdit(step_id=step_id, text=text, done=done) for step_id, text, done in items]


async def test_save_keeps_step_ids_and_times_adds_new_ones_and_deletes_the_rest(
    session: AsyncSession, service: ActionService, clock: Clock
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])
    first, second = action.steps
    clock.advance(minutes=1)
    done_once = await service.save(
        action.public_id,
        1,
        plan(action),
        steps((first.step_id, first.text, True), (second.step_id, second.text, False)),
    )
    clock.advance(minutes=1)

    result = await service.save(
        action.public_id,
        2,
        plan(action),
        steps((None, "Book a review", True), (first.step_id, "Collect all the figures", True)),
    )

    new, kept = result.steps
    assert (new.text, new.done, new.done_at_utc, new.position) == (
        "Book a review",
        True,
        START + timedelta(minutes=2),
        0,
    )
    assert (kept.step_id, kept.text, kept.position) == (first.step_id, "Collect all the figures", 1)
    assert kept.done_at_utc == done_once.steps[0].done_at_utc == START + timedelta(minutes=1)
    assert second.step_id not in {step.step_id for step in result.steps}
    assert await count(session, ActionStepTable) == 2

    undone = await service.save(
        action.public_id, 3, plan(action), steps((kept.step_id, kept.text, False))
    )
    assert [(step.done, step.done_at_utc) for step in undone.steps] == [(False, None)]


@pytest.mark.parametrize("problem", ["unknown", "another_action", "duplicate", "too_many"])
async def test_save_rejects_bad_step_lists(
    session: AsyncSession, service: ActionService, problem: str
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])
    theirs = await service.accept(seeded.suggestion_ids[1])
    theirs = await service.save(theirs.public_id, 1, plan(theirs), steps((None, "Theirs", False)))
    first = action.steps[0].step_id
    bad = {
        "unknown": steps((999_999, "Nope", False)),
        "another_action": steps((theirs.steps[0].step_id, "Theirs", True)),
        "duplicate": steps((first, "A", False), (first, "B", False)),
        "too_many": steps(*((None, f"Step {n}", False) for n in range(31))),
    }[problem]

    with pytest.raises(ActionConflictError):
        await service.save(action.public_id, 1, plan(action), bad)

    assert await service.get(action.public_id) == action
    assert await service.get(theirs.public_id) == theirs


async def test_step_completion_and_action_completion_are_independent(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])

    action = await service.save(
        action.public_id,
        1,
        plan(action),
        steps(*((step.step_id, step.text, True) for step in action.steps)),
    )
    assert action.status is ActionStatus.OPEN

    other = await service.accept(seeded.suggestion_ids[1])
    other = await service.save(
        other.public_id, 1, plan(other), steps((None, "Email facilities", False))
    )
    completed = await service.complete(other.public_id, 2)
    assert [step.done for step in completed.steps] == [False]


# Deleting and restoring


async def test_a_deleted_action_is_hidden_until_restored(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])

    await service.delete(action.public_id, 1)

    with pytest.raises(ActionNotFoundError):
        await service.get(action.public_id)
    assert await service.list_actions(ActionFilter.OPEN) == ()
    assert (await states(session, seeded))[0] == (SuggestionState.DISMISSED, None)

    restored = await service.restore(action.public_id)
    again = await service.restore(action.public_id)

    assert restored == again
    assert restored.revision == 3
    assert (await states(session, seeded))[0] == (SuggestionState.ACCEPTED, action.public_id)
    with pytest.raises(ActionNotFoundError):
        await service.restore("ffffffff-ffff-4fff-8fff-ffffffffffff")


async def test_restoring_a_deleted_completed_action_keeps_it_completed(
    session: AsyncSession, service: ActionService, clock: Clock
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])
    clock.advance(hours=1)
    completed = await service.complete(action.public_id, 1)
    clock.advance(hours=1)
    await service.delete(action.public_id, 2)
    clock.advance(hours=1)

    restored = await service.restore(action.public_id)

    assert (restored.status, restored.completed_at_utc, restored.revision) == (
        ActionStatus.COMPLETED,
        START + timedelta(hours=1),
        4,
    )
    assert restored.completed_at_utc == completed.completed_at_utc
    listed = await service.list_actions(ActionFilter.COMPLETED)
    assert [item.public_id for item in listed] == [action.public_id]


# Lists and carryover


async def test_lists_filter_by_view_and_order_by_target_then_due_then_creation(
    session: AsyncSession, service: ActionService, clock: Clock
) -> None:
    titles = ["No dates", "Target late", "Target early", "Due only", "Waiting"]
    first = await seed(session, tuple(suggestion(n, title) for n, title in enumerate(titles)))
    second = await seed(session, (suggestion(0, "Done"),), message="msg-2")
    actions: dict[str, Action] = {}
    pairs = [*zip(titles, first.suggestion_ids, strict=True), ("Done", second.suggestion_ids[0])]
    for title, suggestion_id in pairs:
        clock.advance(minutes=1)
        actions[title] = await service.accept(suggestion_id)

    def edit(title: str, **values: Any) -> ActionEdit:
        fields: dict[str, Any] = {
            "title": title,
            "ownership": ActionOwnership.MINE,
            "effort": None,
            "target_date": None,
            "notes": "",
        }
        fields.update(values)
        return ActionEdit(**fields)

    await service.save(
        actions["Target late"].public_id, 1, edit("Target late", target_date=date(2026, 10, 9))
    )
    await service.save(
        actions["Target early"].public_id, 1, edit("Target early", target_date=date(2026, 10, 1))
    )
    await service.save(
        actions["Waiting"].public_id, 1, edit("Waiting", ownership=ActionOwnership.WAITING_FOR)
    )
    await service.complete(actions["Done"].public_id, 1)
    await session.execute(
        update(ActionTable)
        .where(ActionTable.public_id == actions["Due only"].public_id)
        .values(
            deadline_text="by Oct 3",
            deadline_precision="date",
            deadline_date=date(2026, 10, 3),
            deadline_timezone="America/Toronto",
        )
    )
    await session.commit()
    session.expunge_all()  # The bulk update bypassed the loaded rows.

    opened = await service.list_actions(ActionFilter.OPEN)
    assert [action.title for action in opened] == [
        "Target early",
        "Target late",
        "Due only",
        "No dates",
    ]
    assert [action.title for action in await service.list_actions(ActionFilter.WAITING)] == [
        "Waiting"
    ]
    assert [action.title for action in await service.list_actions(ActionFilter.COMPLETED)] == [
        "Done"
    ]
    assert len(await service.list_actions(ActionFilter.OPEN, limit=2)) == 2
    with pytest.raises(ValueError):
        await service.list_actions(ActionFilter.OPEN, limit=0)


async def test_completed_actions_are_listed_newest_first(
    session: AsyncSession, service: ActionService, clock: Clock
) -> None:
    seeded = await seed(session)
    older = await service.accept(seeded.suggestion_ids[0])
    newer = await service.accept(seeded.suggestion_ids[1])
    await service.complete(older.public_id, 1)
    clock.advance(minutes=1)
    await service.complete(newer.public_id, 1)

    listed = await service.list_actions(ActionFilter.COMPLETED)

    assert [action.public_id for action in listed] == [newer.public_id, older.public_id]


async def test_an_open_action_is_carried_over_the_next_local_day(
    session: AsyncSession, service: ActionService, clock: Clock
) -> None:
    seeded = await seed(session)
    await service.accept(seeded.suggestion_ids[0])
    created_day = START.astimezone(TORONTO).date()

    (same_day,) = await service.list_actions(ActionFilter.OPEN, limit=1)
    clock.advance(days=1)
    (next_day,) = await service.list_actions(ActionFilter.OPEN, limit=1)

    assert not same_day.carried_over(created_day, TORONTO)
    assert next_day.carried_over(created_day + timedelta(days=1), TORONTO)


# Linking more messages


async def test_accept_into_adds_a_second_source_to_an_existing_action(
    session: AsyncSession, service: ActionService
) -> None:
    first = await seed(session)
    second = await seed(session, (suggestion(0, "Send the deck, again"),), message="msg-2")
    action = await service.accept(first.suggestion_ids[0])

    added = await service.accept_into(second.suggestion_ids[0], action.public_id, 1)
    repeated = await service.accept_into(second.suggestion_ids[0], action.public_id, 1)

    linked = added.action
    assert added.source_added
    assert repeated.action == linked and not repeated.source_added
    assert [source.provider_message_id for source in linked.sources] == ["msg-1", "msg-2"]
    assert linked.revision == 2
    assert (await states(session, second))[0] == (SuggestionState.ACCEPTED, action.public_id)
    assert (await states(session, first))[0] == (SuggestionState.ACCEPTED, action.public_id)


async def test_accept_into_adds_no_source_for_an_email_already_linked(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])

    added = await service.accept_into(seeded.suggestion_ids[1], action.public_id, 1)

    assert not added.source_added
    assert [source.provider_message_id for source in added.action.sources] == ["msg-1"]
    assert added.action.revision == 2
    assert await states(session, seeded) == [
        (SuggestionState.ACCEPTED, action.public_id),
        (SuggestionState.ACCEPTED, action.public_id),
    ]


async def test_accept_into_refuses_deleted_targets_and_suggestions_owned_elsewhere(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    owner = await service.accept(seeded.suggestion_ids[0])
    target = await service.accept(seeded.suggestion_ids[1])

    with pytest.raises(ActionConflictError):
        await service.accept_into(seeded.suggestion_ids[0], target.public_id, 1)

    await service.delete(target.public_id, 1)
    with pytest.raises(ActionConflictError, match="Reopen the action before adding to it."):
        await service.accept_into(seeded.suggestion_ids[0], target.public_id, 2)
    with pytest.raises(ActionNotFoundError):
        await service.accept_into(
            seeded.suggestion_ids[0], "00000000-0000-4000-8000-999999999999", 1
        )
    assert (await service.get(owner.public_id)).revision == 1


async def test_accept_into_refuses_a_completed_action_until_it_is_reopened(
    session: AsyncSession, service: ActionService
) -> None:
    first = await seed(session)
    second = await seed(session, (suggestion(0, "Send the deck, again"),), message="msg-2")
    action = await service.accept(first.suggestion_ids[0])
    await service.complete(action.public_id, 1)
    before = await counts(session)

    with pytest.raises(ActionConflictError, match="Reopen the action before adding to it."):
        await service.accept_into(second.suggestion_ids[0], action.public_id, 2)

    assert await counts(session) == before
    assert (await states(session, second))[0] == (SuggestionState.PENDING, None)
    await service.reopen(action.public_id, 2)
    added = await service.accept_into(second.suggestion_ids[0], action.public_id, 3)
    assert (added.action.revision, added.source_added) == (4, True)


async def test_accept_into_takes_an_email_from_another_thread_when_named(
    session: AsyncSession, service: ActionService
) -> None:
    first = await seed(session)
    other = await seed(session, (suggestion(0, "Other"),), message="msg-2", thread="elsewhere")
    action = await service.accept(first.suggestion_ids[0])

    added = await service.accept_into(other.suggestion_ids[0], action.public_id, 1)

    assert added.source_added
    assert [source.provider_message_id for source in added.action.sources] == ["msg-1", "msg-2"]


async def test_accept_into_takes_over_a_suggestion_whose_action_was_deleted(
    session: AsyncSession, service: ActionService
) -> None:
    first = await seed(session)
    second = await seed(session, (suggestion(0, "Other"),), message="msg-2")
    previous = await service.accept(first.suggestion_ids[0])
    target = await service.accept(second.suggestion_ids[0])
    await service.delete(previous.public_id, 1)

    linked = (await service.accept_into(first.suggestion_ids[0], target.public_id, 1)).action

    assert (linked.public_id, linked.revision) == (target.public_id, 2)
    assert [source.provider_message_id for source in linked.sources] == ["msg-2", "msg-1"]
    assert (await states(session, first))[0] == (SuggestionState.ACCEPTED, target.public_id)
    assert await count(session, SuggestionDecisionTable) == 2  # Re-pointed, not added.


# Undoing an addition


async def added_into(
    session: AsyncSession, service: ActionService
) -> tuple[Seeded, Seeded, Action]:
    """An action from msg-1, with msg-2's suggestion added into it (revision 2)."""
    first = await seed(session)
    second = await seed(session, (suggestion(0, "Send the deck, again"),), message="msg-2")
    action = await service.accept(first.suggestion_ids[0])
    added = await service.accept_into(second.suggestion_ids[0], action.public_id, 1)
    assert added.source_added
    return first, second, added.action


@pytest.mark.parametrize("remove_source", [True, False])
async def test_undo_add_returns_the_suggestion_to_pending_in_one_revision(
    session: AsyncSession, service: ActionService, clock: Clock, remove_source: bool
) -> None:
    first, second, action = await added_into(session, service)
    clock.advance(minutes=5)

    undone = await service.undo_accept_into(
        second.suggestion_ids[0], action.public_id, 2, remove_source
    )

    assert (undone.revision, undone.updated_at_utc) == (3, clock.now)
    kept = ["msg-1"] if remove_source else ["msg-1", "msg-2"]
    assert [source.provider_message_id for source in undone.sources] == kept
    assert await service.get(action.public_id) == undone
    assert (await states(session, second))[0] == (SuggestionState.PENDING, None)
    assert (await states(session, first))[0] == (SuggestionState.ACCEPTED, action.public_id)
    assert await count(session, SuggestionDecisionTable) == 1


async def test_undo_add_never_removes_the_last_source(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])
    await service.accept_into(seeded.suggestion_ids[1], action.public_id, 1)

    undone = await service.undo_accept_into(seeded.suggestion_ids[1], action.public_id, 2, True)

    assert [source.provider_message_id for source in undone.sources] == ["msg-1"]
    assert undone.revision == 3
    assert await states(session, seeded) == [
        (SuggestionState.ACCEPTED, action.public_id),
        (SuggestionState.PENDING, None),
    ]


@pytest.mark.parametrize("change", ["edited", "pending", "other_action", "deleted"])
async def test_undo_add_is_refused_once_anything_changed(
    session: AsyncSession, service: ActionService, change: str
) -> None:
    first, second, action = await added_into(session, service)
    suggestion_id, revision = second.suggestion_ids[0], 2
    if change == "edited":
        await service.complete(action.public_id, 2)
    elif change == "pending":
        suggestion_id = first.suggestion_ids[1]  # Never added: it has no decision.
    elif change == "other_action":
        other = await service.accept(first.suggestion_ids[1])
        suggestion_id = first.suggestion_ids[1]
        assert other.public_id != action.public_id
    else:
        await service.delete(action.public_id, 2)
        revision = 3
    before = await counts(session)

    with pytest.raises(ActionConflictError, match="Only an unchanged addition can be undone."):
        await service.undo_accept_into(suggestion_id, action.public_id, revision, True)

    assert await counts(session) == before  # No decision or source was removed.
    if change != "deleted":
        expected = 3 if change == "edited" else 2  # Completing was the only change.
        assert (await service.get(action.public_id)).revision == expected


# Durability


async def test_deleting_the_account_keeps_the_action_with_its_source_unavailable(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])

    await session.execute(delete(AccountTable))
    await session.commit()
    session.expunge_all()

    kept = await service.get(action.public_id)
    (source,) = kept.sources
    assert (source.available, source.in_inbox, source.subject) == (False, None, "Subject of msg-1")
    assert await count(session, SuggestionDecisionTable) == 1  # Kept for a reconnect.
    assert await count(session, ActionSuggestionTable) == 0


async def delete_message(session: AsyncSession, seeded: Seeded) -> None:
    """Delete a cached message row; its analysis and suggestions go with it."""
    await session.execute(delete(MessageTable).where(MessageTable.id == seeded.message_id))
    await session.commit()
    session.expunge_all()


async def test_an_acceptance_survives_deleting_and_resyncing_its_message(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    await seed(session, (suggestion(0, "Other"),), message="msg-2")  # Holds the highest row ID.
    action = await service.accept(seeded.suggestion_ids[0])
    await delete_message(session, seeded)

    again = await seed(session)  # The same Gmail message and suggestion titles.

    assert again.message_id != seeded.message_id
    assert await states(session, again) == [
        (SuggestionState.ACCEPTED, action.public_id),
        (SuggestionState.PENDING, None),
    ]
    accepted = await service.accept(again.suggestion_ids[0])
    assert (accepted.public_id, accepted.revision) == (action.public_id, 1)
    opened = await service.list_actions(ActionFilter.OPEN)
    assert [item.public_id for item in opened] == [action.public_id]


async def test_a_dismissal_survives_deleting_and_resyncing_its_message(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    await seed(session, (suggestion(0, "Other"),), message="msg-2")
    await service.dismiss(seeded.suggestion_ids[1])
    await delete_message(session, seeded)

    again = await seed(session)

    assert again.message_id != seeded.message_id
    assert await states(session, again) == [
        (SuggestionState.PENDING, None),
        (SuggestionState.DISMISSED, None),
    ]
    assert await count(session, SuggestionDecisionTable) == 1


async def test_decisions_survive_deleting_and_reconnecting_the_account(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])
    await service.dismiss(seeded.suggestion_ids[1])
    account_id = await session.scalar(select(AccountTable.id))
    assert account_id is not None
    assert await AccountRepository(session).delete_by_id(account_id)
    await session.commit()
    session.expunge_all()

    again = await seed(session)  # Reconnect the same Gmail account and sync again.

    assert await states(session, again) == [
        (SuggestionState.ACCEPTED, action.public_id),
        (SuggestionState.DISMISSED, None),
    ]
    assert (await service.accept(again.suggestion_ids[0])).public_id == action.public_id
    assert len(await service.list_actions(ActionFilter.OPEN)) == 1
    assert await count(session, SuggestionDecisionTable) == 2


async def test_an_action_survives_closing_and_reopening_the_database(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    first = Database.from_path(path)
    await first.create_schema_for_tests()
    try:
        async with first.session() as session:
            seeded = await seed(session)
            accepted = await ActionService(session).accept(seeded.suggestion_ids[0])
    finally:
        await first.dispose()

    second = Database.from_path(path)
    try:
        async with second.session() as session:
            assert await ActionService(session).get(accepted.public_id) == accepted
    finally:
        await second.dispose()


@pytest.fixture
def failing_source(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def fail(self: ActionRepository, action_id: int, message: Any, account: Any) -> bool:
        raise RuntimeError("disk full")

    monkeypatch.setattr(ActionRepository, "add_source", fail)
    yield


async def test_a_failure_during_accept_leaves_nothing_behind(
    session: AsyncSession, service: ActionService, failing_source: None
) -> None:
    seeded = await seed(session)

    with pytest.raises(RuntimeError):
        await service.accept(seeded.suggestion_ids[0])

    assert await counts(session) == (0, 0, 0, 0)


async def test_the_clock_must_be_aware(session: AsyncSession) -> None:
    seeded = await seed(session)
    naive = ActionService(session, clock=lambda: datetime(2026, 9, 28, 12, 0))

    with pytest.raises(ValueError):
        await naive.accept(seeded.suggestion_ids[0])


async def test_save_applies_an_edit_and_a_new_plan_as_one_revision(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])
    edit = ActionEdit(
        title="Send the final deck",
        ownership=ActionOwnership.WAITING_FOR,
        effort=None,
        target_date=None,
        notes="",
    )

    saved = await service.save(
        action.public_id,
        1,
        edit,
        [StepEdit(step_id=action.steps[1].step_id, text="Draft the slides", done=False)],
    )
    edit_only = await service.save(action.public_id, 2, edit)

    assert (saved.revision, saved.title, saved.ownership) == (
        2,
        "Send the final deck",
        ActionOwnership.WAITING_FOR,
    )
    assert [step.text for step in saved.steps] == ["Draft the slides"]
    assert edit_only.revision == 3
    assert edit_only.steps == saved.steps


async def test_save_changes_nothing_when_its_plan_is_rejected(
    session: AsyncSession, service: ActionService
) -> None:
    seeded = await seed(session)
    action = await service.accept(seeded.suggestion_ids[0])
    edit = ActionEdit(
        title="Should not stick",
        ownership=ActionOwnership.MINE,
        effort=None,
        target_date=None,
        notes="",
    )

    with pytest.raises(ActionConflictError):
        await service.save(
            action.public_id, 1, edit, [StepEdit(step_id=999_999, text="Not mine", done=False)]
        )
    with pytest.raises(ActionConflictError):
        await service.save(action.public_id, 7, edit)

    assert await service.get(action.public_id) == action


async def add_bare_actions(
    session: AsyncSession, count: int, *, status: str, ownership: str = "mine", start: int = 0
) -> None:
    """Actions straight into storage, completed a minute apart when completed."""
    for index in range(start, start + count):
        done = status == ActionStatus.COMPLETED.value
        session.add(
            ActionTable(
                public_id=f"00000000-0000-4000-8000-{index:012d}",
                title=f"Action {index}",
                ownership=ownership,
                status=status,
                deadline_precision="none",
                notes="",
                created_at_utc=START,
                updated_at_utc=START,
                completed_at_utc=START + timedelta(minutes=index) if done else None,
                revision=1,
            )
        )
    await session.commit()


async def test_open_and_waiting_lists_are_never_capped(
    session: AsyncSession, service: ActionService
) -> None:
    await add_bare_actions(session, 205, status="open")
    await add_bare_actions(session, 203, status="open", ownership="waiting_for", start=205)

    assert len(await service.list_actions(ActionFilter.OPEN)) == 205
    assert len(await service.list_actions(ActionFilter.WAITING)) == 203
    assert await service.count_actions(ActionFilter.OPEN) == 205
    assert await service.count_actions(ActionFilter.WAITING) == 203
    assert await service.count_actions(ActionFilter.COMPLETED) == 0


async def test_the_completed_list_shows_the_newest_200_and_counts_the_rest(
    session: AsyncSession, service: ActionService
) -> None:
    await add_bare_actions(session, 203, status="completed")
    deleted = await session.scalar(select(ActionTable).where(ActionTable.title == "Action 202"))
    assert deleted is not None
    deleted.deleted_at_utc = START
    await session.commit()

    listed = await service.list_actions(ActionFilter.COMPLETED)

    assert len(listed) == 200
    assert (listed[0].title, listed[-1].title) == ("Action 201", "Action 2")
    assert await service.count_actions(ActionFilter.COMPLETED) == 202  # Not the deleted one.
    assert len(await service.list_actions(ActionFilter.COMPLETED, limit=5)) == 5
    rows = await ActionRepository(session).list_rows(ActionFilter.COMPLETED, limit=None)
    assert len(rows) == 202  # Storage lists them all when asked without a cap.
