"""Stored suggestions and the owner's decisions, read back as suggestion views."""

import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, event, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import SuggestionState
from mailbrief.domain.analysis import ActionSuggestion
from mailbrief.domain.messages import AccountIdentity, EmailContact, ProviderKind
from mailbrief.services.actions import ActionService, SuggestionNotFoundError
from mailbrief.storage import database as storage_database
from mailbrief.storage.actions import ActionRepository, DecisionKey, suggestion_views
from mailbrief.storage.database import MAX_SQLITE_BATCH_SIZE, Database
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


async def messages(session: AsyncSession, count: int, *, account: str = "gmail-1") -> list[int]:
    """Messages msg-0, msg-1 and so on in one Gmail account."""
    row = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.GMAIL,
            provider_account_id=account,
            email_address=f"{account}@x.com",
        )
    )
    rows = await MessageRepository(session).upsert_messages(
        row.id, [make_message(provider_message_id=f"msg-{index}") for index in range(count)]
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


async def key_for(session: AsyncSession, message_id: int, title: str) -> DecisionKey:
    """The key a decision on this message's suggestion is kept under."""
    found = await session.execute(
        select(
            AccountTable.provider,
            AccountTable.provider_account_id,
            MessageTable.provider_message_id,
        )
        .join(AccountTable, MessageTable.account_id == AccountTable.id)
        .where(MessageTable.id == message_id)
    )
    provider, account_id, provider_message_id = found.tuples().one()
    return DecisionKey(provider, account_id, provider_message_id, fingerprint_of(title))


async def decide(
    session: AsyncSession,
    message_id: int,
    title: str,
    decision: str,
    action_id: int | None = None,
) -> None:
    key = await key_for(session, message_id, title)
    await session.execute(
        insert(SuggestionDecisionTable).values(
            provider=key.provider,
            provider_account_id=key.provider_account_id,
            provider_message_id=key.provider_message_id,
            fingerprint=key.fingerprint,
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

    # Suggestions, message identities, decisions and actions.
    assert len(selects) == 4
    assert [len(views[key]) for key, _ in pairs] == [5, 5, 1]


async def test_large_requests_are_chunked(
    database: Database, selects: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_database, "MAX_SQLITE_BATCH_SIZE", 2)
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

    # Five messages give three chunks each of suggestions, identities and decisions. Six
    # accepted decisions keep an action (C, D and one per other message): three more chunks.
    assert len(selects) == 12
    assert states(views[message_id]) == EVERY_STATE
    for other in others:
        assert states(views[other]) == [("A", SuggestionState.ACCEPTED, f"{other:036d}")]


async def test_views_stay_correct_past_one_batch(database: Database, selects: list[str]) -> None:
    count = MAX_SQLITE_BATCH_SIZE + 5
    expected: dict[int, tuple[SuggestionState, str | None]] = {}
    async with database.session() as session:
        pairs: list[tuple[int, int]] = []
        for index, message_id in enumerate(await messages(session, count)):
            pairs.append((message_id, await analysis_with(session, message_id, FIVE[:1])))
            if index % 3 == 1:
                await decide(session, message_id, "A", "dismissed")
                expected[message_id] = (SuggestionState.DISMISSED, None)
            elif index % 3 == 2:
                public_id = f"{message_id:036d}"
                await decide(session, message_id, "A", "accepted", await action(session, public_id))
                expected[message_id] = (SuggestionState.ACCEPTED, public_id)
            else:
                expected[message_id] = (SuggestionState.PENDING, None)
        await session.commit()
        selects.clear()

        views = await suggestion_views(session, pairs)

    # Two chunks each of suggestions, identities and decisions; 35 actions fit in one.
    assert len(selects) == 7
    assert {
        message_id: (view.state, view.action_public_id) for message_id, (view,) in views.items()
    } == expected


async def test_a_decision_from_another_account_never_matches(database: Database) -> None:
    async with database.session() as session:
        (mine,) = await messages(session, 1, account="gmail-1")
        # gmail-2 has the same Gmail message ID, msg-0, and one more, msg-1.
        theirs, their_next = await messages(session, 2, account="gmail-2")
        analyses = {
            message_id: await analysis_with(session, message_id, FIVE[:2])
            for message_id in (mine, theirs, their_next)
        }
        await decide(session, theirs, "A", "dismissed")
        await decide(session, theirs, "B", "accepted", await action(session, LIVE_ID))
        await session.commit()

        both = await suggestion_views(session, [(mine, analyses[mine]), (theirs, analyses[theirs])])
        # gmail-1's msg-0 and gmail-2's msg-1 also read gmail-2's msg-0 decisions; neither
        # message matches them.
        crossed = await suggestion_views(
            session, [(mine, analyses[mine]), (their_next, analyses[their_next])]
        )

    pending = [("A", SuggestionState.PENDING, None), ("B", SuggestionState.PENDING, None)]
    assert states(both[mine]) == pending
    assert states(both[theirs]) == [
        ("A", SuggestionState.DISMISSED, None),
        ("B", SuggestionState.ACCEPTED, LIVE_ID),
    ]
    assert states(crossed[mine]) == states(crossed[their_next]) == pending


async def test_resaving_an_analysis_never_reuses_a_suggestion_id(database: Database) -> None:
    async with database.session() as session:
        (message_id,) = await messages(session, 1)
        analysis_id = await analysis_with(session, message_id, FIVE)
        await session.commit()
        old_ids = list(await session.scalars(select(ActionSuggestionTable.id)))

        # The same cache identity, saved again with other suggestions, replaces the rows.
        again = await analysis_with(session, message_id, (suggestion(0, "F"), suggestion(1, "G")))
        await session.commit()
        new_ids = list(await session.scalars(select(ActionSuggestionTable.id)))

        service = ActionService(session)
        for old_id in old_ids:
            with pytest.raises(SuggestionNotFoundError):
                await service.accept(old_id)
        remaining = await session.scalar(select(func.count()).select_from(ActionTable))

    assert again == analysis_id
    assert len(new_ids) == 2
    assert min(new_ids) > max(old_ids)
    assert remaining == 0


async def test_loading_many_actions_takes_a_fixed_number_of_queries(
    database: Database, selects: list[str]
) -> None:
    async with database.session() as session:
        (message_id,) = await messages(session, 1)
        message = await session.get(MessageTable, message_id)
        assert message is not None
        owner = await session.get(AccountTable, message.account_id)
        assert owner is not None
        public_ids = (LIVE_ID, SOFT_DELETED_ID, HARD_DELETED_ID)
        rows = [await session.get(ActionTable, await action(session, key)) for key in public_ids]
        repository = ActionRepository(session)
        for row in rows:
            assert row is not None
            for position in range(3):
                session.add(
                    ActionStepTable(
                        action_id=row.id, position=position, text=f"Step {position}", done=False
                    )
                )
            assert await repository.add_source(row.id, message, owner)
            assert not await repository.add_source(row.id, message, owner)  # Already linked.
        await session.commit()
        selects.clear()

        loaded = await repository.load([row for row in rows if row is not None])

    # Steps, sources, the accounts their snapshots name, those accounts' thread messages and
    # pending proposals: one query each, however many actions load.
    assert len(selects) == 5
    assert [len(item.steps) for item in loaded] == [3, 3, 3]
    assert [[source.available for source in item.sources] for item in loaded] == [[True]] * 3


async def test_pending_cannot_be_saved_as_a_decision(database: Database) -> None:
    async with database.session() as session:
        (message_id,) = await messages(session, 1)

        with pytest.raises(ValueError, match="absence of a decision"):
            await ActionRepository(session).save_decision(
                await key_for(session, message_id, "A"),
                decision=SuggestionState.PENDING,
                action_id=None,
                decided_at_utc=NOW,
            )
        stored = await session.scalar(select(func.count()).select_from(SuggestionDecisionTable))

    assert stored == 0


async def test_deleting_a_decision_that_was_never_stored_changes_nothing(
    database: Database,
) -> None:
    async with database.session() as session:
        message_id, analysis_id = await seed_every_state(session)
        repository = ActionRepository(session)

        pending = await key_for(session, message_id, "A")
        dismissed = await key_for(session, message_id, "B")
        await repository.delete_decision(pending)
        await repository.delete_decision(replace(dismissed, provider_message_id="msg-other"))
        await repository.delete_decision(replace(dismissed, provider_account_id="gmail-2"))
        await session.commit()

        views = await suggestion_views(session, [(message_id, analysis_id)])
        stored = await session.scalar(select(func.count()).select_from(SuggestionDecisionTable))

    assert states(views[message_id]) == EVERY_STATE
    assert stored == 4


async def test_a_source_copies_the_message_row_exactly(database: Database) -> None:
    received = datetime(2026, 9, 26, 23, 59, 59, 999_999, tzinfo=UTC)
    async with database.session() as session:
        account = await AccountRepository(session).upsert(
            AccountIdentity(
                provider=ProviderKind.GMAIL, provider_account_id="gmail-1", email_address="me@x.com"
            )
        )
        (message,) = await MessageRepository(session).upsert_messages(
            account.id,
            [
                make_message(
                    provider_message_id="msg-snapshot",
                    subject="  Re: Budget — ✅ “final” 🚀  ",
                    sender=EmailContact(name="Ana", address="Ana.Ops+q3@Example.COM"),
                    received_at_utc=received,
                    web_link="https://mail.google.com/mail/u/?authuser=me%40x.com#all/msg-snapshot",
                )
            ],
        )
        action_id = await action(session, LIVE_ID)
        assert await ActionRepository(session).add_source(action_id, message, account)
        await session.commit()
        session.expunge_all()  # Compare what was written, not the objects in memory.

        stored = await session.get(MessageTable, message.id)
        source = await session.scalar(select(ActionSourceTable))

    assert stored is not None
    assert source is not None
    assert (
        source.message_id,
        source.provider_message_id,
        source.subject,
        source.sender_address,
        source.web_link,
        source.received_at_utc,
    ) == (
        stored.id,
        stored.provider_message_id,
        stored.subject,
        stored.sender_address,
        stored.web_link,
        stored.received_at_utc,
    )
    assert source.received_at_utc == received
    assert (source.provider, source.provider_account_id, source.provider_thread_id) == (
        "gmail",
        "gmail-1",
        stored.conversation_id,
    )
    assert stored.conversation_id == "conversation-1"


async def test_sent_messages_are_stored_and_read_back(database: Database) -> None:
    async with database.session() as session:
        account = await AccountRepository(session).upsert(
            AccountIdentity(
                provider=ProviderKind.GMAIL, provider_account_id="gmail-1", email_address="me@x.com"
            )
        )
        repository = MessageRepository(session)
        (sent,) = await repository.upsert_messages(
            account.id, [make_message(provider_message_id="mine", is_sent=True, is_in_inbox=False)]
        )
        (received,) = await repository.upsert_messages(
            account.id, [make_message(provider_message_id="theirs")]
        )
        await session.commit()

        assert (sent.is_sent, received.is_sent) == (True, False)
        restored = MessageRepository.to_domain(sent, "gmail-1", ProviderKind.GMAIL)
        assert restored.is_sent and not restored.is_in_inbox
        (updated,) = await repository.upsert_messages(
            account.id, [make_message(provider_message_id="mine", is_sent=False)]
        )
        assert not updated.is_sent
