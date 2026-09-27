"""Stored suggestions and the owner's decisions, read back as suggestion views."""

import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, event, insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import SuggestionState
from mailbrief.domain.analysis import ActionSuggestion
from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.storage import actions as storage_actions
from mailbrief.storage.actions import suggestion_views
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, AnalysisRepository, MessageRepository
from mailbrief.storage.tables import ActionSuggestionTable, ActionTable, SuggestionDecisionTable
from tests.factories import fingerprint_of, make_analysis, make_message, make_suggestion

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
LIVE_ID = "11111111-1111-4111-8111-111111111111"
SOFT_DELETED_ID = "22222222-2222-4222-8222-222222222222"
HARD_DELETED_ID = "33333333-3333-4333-8333-333333333333"


def suggestion(position: int, title: str) -> ActionSuggestion:
    return make_suggestion(position=position, title=title, fingerprint=fingerprint_of(title))


FIVE = tuple(suggestion(index, title) for index, title in enumerate("ABCDE"))


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.from_path(tmp_path / "actions.sqlite3")
    await database.create_schema_for_tests()
    try:
        yield database
    finally:
        await database.dispose()


async def messages(session: AsyncSession, count: int) -> list[int]:
    account = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.GMAIL, provider_account_id="gmail-1", email_address="me@x.com"
        )
    )
    rows = await MessageRepository(session).upsert_messages(
        account.id, [make_message(provider_message_id=f"msg-{index}") for index in range(count)]
    )
    return [row.id for row in rows]


async def analysis_with(
    session: AsyncSession, message_id: int, suggestions: tuple[ActionSuggestion, ...]
) -> int:
    row = await AnalysisRepository(session).upsert_analysis(
        message_id=message_id,
        input_hash=f"hash-{message_id}",
        provider="groq",
        model="model-1",
        prompt_version="prompt-1",
        schema_version="6",
        analysis=make_analysis(suggestions=suggestions),
    )
    return row.id


async def action(session: AsyncSession, public_id: str, *, deleted: bool = False) -> int:
    action_id = await session.scalar(
        insert(ActionTable)
        .values(
            public_id=public_id,
            title="Accepted action",
            ownership="mine",
            status="open",
            created_at_utc=NOW,
            updated_at_utc=NOW,
            deleted_at_utc=NOW if deleted else None,
        )
        .returning(ActionTable.id)
    )
    assert action_id is not None
    return action_id


async def decide(
    session: AsyncSession,
    message_id: int,
    title: str,
    decision: str,
    action_id: int | None = None,
) -> None:
    await session.execute(
        insert(SuggestionDecisionTable).values(
            message_id=message_id,
            fingerprint=fingerprint_of(title),
            decision=decision,
            action_id=action_id,
            decided_at_utc=NOW,
        )
    )


async def seed_every_state(session: AsyncSession) -> tuple[int, int]:
    """One message with suggestions A-E: none, dismissed and three accepted decisions."""
    (message_id,) = await messages(session, 1)
    analysis_id = await analysis_with(session, message_id, FIVE)
    await decide(session, message_id, "B", "dismissed")
    await decide(session, message_id, "C", "accepted", await action(session, LIVE_ID))
    await decide(
        session, message_id, "D", "accepted", await action(session, SOFT_DELETED_ID, deleted=True)
    )
    gone = await action(session, HARD_DELETED_ID)
    await decide(session, message_id, "E", "accepted", gone)
    await session.execute(delete(ActionTable).where(ActionTable.id == gone))
    await session.commit()
    return message_id, analysis_id


def states(views: tuple[Any, ...]) -> list[tuple[str, SuggestionState, str | None]]:
    return [(view.suggestion.title, view.state, view.action_public_id) for view in views]


EVERY_STATE = [
    ("A", SuggestionState.PENDING, None),
    ("B", SuggestionState.DISMISSED, None),
    ("C", SuggestionState.ACCEPTED, LIVE_ID),
    ("D", SuggestionState.DISMISSED, None),
    ("E", SuggestionState.PENDING, None),
]


async def test_each_decision_gives_its_state(database: Database) -> None:
    async with database.session() as session:
        message_id, analysis_id = await seed_every_state(session)

        views = await suggestion_views(session, [(message_id, analysis_id)])

    assert states(views[message_id]) == EVERY_STATE


async def test_a_dismissal_belongs_to_its_message_only(database: Database) -> None:
    async with database.session() as session:
        first, second = await messages(session, 2)
        pairs = [
            (first, await analysis_with(session, first, FIVE[:1])),
            (second, await analysis_with(session, second, FIVE[:1])),
        ]
        await decide(session, first, "A", "dismissed")
        await session.commit()

        views = await suggestion_views(session, pairs)

    assert states(views[first]) == [("A", SuggestionState.DISMISSED, None)]
    assert states(views[second]) == [("A", SuggestionState.PENDING, None)]


async def test_every_message_gets_an_entry_even_without_suggestions(database: Database) -> None:
    async with database.session() as session:
        without_analysis, without_rows, with_rows = await messages(session, 3)
        pairs = [
            (without_analysis, None),
            (without_rows, await analysis_with(session, without_rows, ())),
            (with_rows, await analysis_with(session, with_rows, FIVE[:2])),
        ]
        await session.commit()

        views = await suggestion_views(session, pairs)
        empty = await suggestion_views(session, [])

    assert views[without_analysis] == ()
    assert views[without_rows] == ()
    assert [view.suggestion.title for view in views[with_rows]] == ["A", "B"]
    assert empty == {}


async def test_views_follow_position_order(database: Database) -> None:
    async with database.session() as session:
        (message_id,) = await messages(session, 1)
        analysis_id = await analysis_with(session, message_id, ())
        for position, title in ((2, "Third"), (0, "First"), (1, "Second")):
            row = suggestion(position, title)
            await session.execute(
                insert(ActionSuggestionTable).values(
                    analysis_id=analysis_id,
                    position=position,
                    title=title,
                    ownership=row.ownership.value,
                    steps_json=list(row.steps),
                    evidence=row.evidence,
                    fingerprint=row.fingerprint,
                )
            )
        await session.commit()

        views = await suggestion_views(session, [(message_id, analysis_id)])

    assert [view.suggestion.title for view in views[message_id]] == ["First", "Second", "Third"]
    assert [view.suggestion.position for view in views[message_id]] == [0, 1, 2]
    # Inserted as Third, First, Second: the order is by position, not by row ID.
    assert [view.suggestion_id for view in views[message_id]] == [2, 3, 1]


async def test_a_stored_row_that_no_longer_validates_is_skipped_and_counted(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="mailbrief.storage.actions")
    async with database.session() as session:
        (message_id,) = await messages(session, 1)
        analysis_id = await analysis_with(session, message_id, FIVE[:3])
        await session.execute(
            update(ActionSuggestionTable)
            .where(ActionSuggestionTable.position == 1)
            .values(title="t" * 121)
        )
        await session.execute(
            update(ActionSuggestionTable)
            .where(ActionSuggestionTable.position == 2)
            .values(steps_json={"not": "a list"})
        )
        await session.commit()

        views = await suggestion_views(session, [(message_id, analysis_id)])

    assert [view.suggestion.title for view in views[message_id]] == ["A"]
    messages_logged = [record.getMessage() for record in caplog.records]
    assert messages_logged == ["Skipped 2 stored suggestions that no longer validate"]
    assert "t" * 20 not in caplog.text


async def test_views_survive_closing_and_reopening_the_database(tmp_path: Path) -> None:
    path = tmp_path / "reopened.sqlite3"
    database = Database.from_path(path)
    await database.create_schema_for_tests()
    try:
        async with database.session() as session:
            message_id, analysis_id = await seed_every_state(session)
            before = await suggestion_views(session, [(message_id, analysis_id)])
    finally:
        await database.dispose()

    reopened = Database.from_path(path)
    try:
        async with reopened.session() as session:
            after = await suggestion_views(session, [(message_id, analysis_id)])
    finally:
        await reopened.dispose()

    assert after == before
    assert states(after[message_id]) == EVERY_STATE


@pytest.fixture
def selects(database: Database) -> Iterator[list[str]]:
    """The SELECT statements the database runs while the test uses it."""
    statements: list[str] = []

    def record(*args: Any) -> None:
        statement = str(args[2])
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    yield statements
    event.remove(engine, "before_cursor_execute", record)


async def test_views_take_one_query_per_kind_of_row(database: Database, selects: list[str]) -> None:
    async with database.session() as session:
        message_id, analysis_id = await seed_every_state(session)
        second, third = (await messages(session, 3))[1:]
        pairs = [
            (message_id, analysis_id),
            (second, await analysis_with(session, second, FIVE)),
            (third, await analysis_with(session, third, FIVE[:1])),
        ]
        await session.commit()
        selects.clear()

        views = await suggestion_views(session, pairs)

    assert len(selects) == 3
    assert [len(views[key]) for key, _ in pairs] == [5, 5, 1]


async def test_large_requests_are_chunked(
    database: Database, selects: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_actions, "MAX_SQLITE_BATCH_SIZE", 2)
    async with database.session() as session:
        message_id, analysis_id = await seed_every_state(session)
        others = (await messages(session, 5))[1:]
        pairs = [(message_id, analysis_id)]
        for other in others:
            pairs.append((other, await analysis_with(session, other, FIVE[:1])))
            await decide(session, other, "A", "accepted", await action(session, f"{other:036d}"))
        await session.commit()
        selects.clear()

        views = await suggestion_views(session, pairs)

    # Five messages give three chunks each of suggestions and decisions. Six accepted
    # decisions keep an action (C, D and one per other message): three more chunks.
    assert len(selects) == 9
    assert states(views[message_id]) == EVERY_STATE
    for other in others:
        assert states(views[other]) == [("A", SuggestionState.ACCEPTED, f"{other:036d}")]
