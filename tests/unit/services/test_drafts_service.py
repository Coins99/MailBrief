"""Drafts: creation, replies, action links, autosave, versions, restore, delete and durability."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.actions import ActionStatus
from mailbrief.domain.drafts import (
    MAX_DRAFT_VERSIONS,
    Draft,
    DraftEdit,
    DraftKind,
    DraftVersionOrigin,
)
from mailbrief.domain.messages import AccountIdentity, EmailContact, ProviderKind
from mailbrief.services.actions import ActionNotFoundError, ActionService
from mailbrief.services.drafts import (
    DraftConflictError,
    DraftNotFoundError,
    DraftService,
    SourceNotFoundError,
    reply_title,
)
from mailbrief.storage.actions import ActionRepository
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, MessageRepository
from mailbrief.storage.tables import (
    AccountTable,
    ActionTable,
    DraftSourceTable,
    DraftTable,
    DraftVersionTable,
    MessageTable,
)
from tests.factories import make_message

START = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
OWNER = "me@x.com"


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


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.from_path(tmp_path / "drafts.sqlite3")
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
def service(session: AsyncSession, clock: Clock) -> DraftService:
    return DraftService(session, clock=clock, id_factory=Ids())


async def seed_message(
    session: AsyncSession,
    message: str = "msg-1",
    *,
    subject: str = "Budget review",
    sender: str = "alex@example.com",
) -> MessageTable:
    account = await AccountRepository(session).upsert(
        AccountIdentity(
            provider=ProviderKind.GMAIL, provider_account_id="gmail-1", email_address=OWNER
        )
    )
    (row,) = await MessageRepository(session).upsert_messages(
        account.id,
        [
            make_message(
                provider=ProviderKind.GMAIL,
                provider_account_id="gmail-1",
                provider_message_id=message,
                subject=subject,
                sender=EmailContact(address=sender),
                body_preview="Private preview text",
                web_link=f"https://mail.google.com/mail/u/?authuser=me%40x.com#all/{message}",
            )
        ],
    )
    await session.commit()
    return row


async def seed_action(
    session: AsyncSession, messages: list[MessageTable], *, title: str = "Send the deck"
) -> ActionTable:
    repository = ActionRepository(session)
    row = await repository.add_action(
        ActionTable(
            public_id="11111111-1111-4111-8111-111111111111",
            title=title,
            ownership="mine",
            status=ActionStatus.OPEN.value,
            deadline_precision="none",
            notes="",
            created_at_utc=START,
            updated_at_utc=START,
            revision=1,
        )
    )
    for message in messages:
        account = await session.get(AccountTable, message.account_id)
        assert account is not None
        await repository.add_source(row.id, message, account)
    await session.commit()
    return row


async def count(session: AsyncSession, table: type[Any]) -> int:
    return (await session.scalar(select(func.count()).select_from(table))) or 0


async def fresh(database: Database, public_id: str) -> Draft:
    """The draft as a new session reads it."""
    async with database.session() as other:
        return await DraftService(other).get(public_id)


@pytest.mark.parametrize("kind", list(DraftKind))
async def test_create_makes_a_blank_draft_with_a_first_version(
    service: DraftService, kind: DraftKind
) -> None:
    draft = await service.create(kind)

    assert (draft.kind, draft.revision, draft.body, draft.sources) == (kind, 1, "", ())
    assert (draft.created_at_utc, draft.updated_at_utc) == (START, START)
    (version,) = await service.versions(draft.public_id)
    assert (version.number, version.origin, version.length) == (1, DraftVersionOrigin.CREATED, 0)


async def test_create_reply_uses_only_the_cached_message(
    service: DraftService, session: AsyncSession
) -> None:
    message = await seed_message(session)

    draft = await service.create_reply(OWNER, "msg-1")

    assert (draft.kind, draft.title, draft.to_text, draft.cc_text, draft.body) == (
        DraftKind.REPLY,
        "Re: Budget review",
        "alex@example.com",
        "",
        "",
    )
    (source,) = draft.sources
    assert (source.provider_message_id, source.subject, source.available) == (
        "msg-1",
        "Budget review",
        True,
    )
    assert source.received_at_utc == message.received_at_utc
    assert "Private preview" not in draft.model_dump_json()
    version = await service.version(draft.public_id, 1)
    assert (version.title, version.to_text) == ("Re: Budget review", "alex@example.com")


async def test_a_reply_to_an_unknown_sender_leaves_to_empty(
    service: DraftService, session: AsyncSession
) -> None:
    await seed_message(session, sender="unknown@invalid")

    draft = await service.create_reply(OWNER, "msg-1")

    assert draft.to_text == ""
    assert draft.sources[0].sender_address == "unknown@invalid"


@pytest.mark.parametrize(("account", "message"), [("other@x.com", "msg-1"), (OWNER, "gone")])
async def test_a_reply_needs_the_message_in_local_mail(
    service: DraftService, session: AsyncSession, account: str, message: str
) -> None:
    await seed_message(session)

    with pytest.raises(SourceNotFoundError, match="^That email is no longer in local mail.$"):
        await service.create_reply(account, message)
    assert await count(session, DraftTable) == 0


@pytest.mark.parametrize(
    ("subject", "title"),
    [
        ("Budget", "Re: Budget"),
        ("Re: Budget", "Re: Budget"),
        ("RE: Budget", "RE: Budget"),
        ("re:Budget", "re:Budget"),
        ("Re：予算", "Re：予算"),
        ("Re : spaced", "Re : spaced"),
        ("Regarding plans", "Re: Regarding plans"),
        ("", "Re:"),
        ("  Two\nlines‮ ", "Re: Two lines"),
        ("x" * 300, ("Re: " + "x" * 300)[:200]),
    ],
)
def test_reply_title(subject: str, title: str) -> None:
    assert reply_title(subject) == title


@pytest.mark.parametrize("kind", [DraftKind.EMAIL, DraftKind.NOTE, DraftKind.MESSAGE])
async def test_a_draft_for_an_action_takes_its_title_link_and_sources(
    service: DraftService, session: AsyncSession, kind: DraftKind
) -> None:
    first = await seed_message(session, "msg-1")
    second = await seed_message(session, "msg-2", subject="Follow-up")
    action = await seed_action(session, [first, second])

    draft = await service.create_for_action(action.public_id, kind)

    assert (draft.kind, draft.title, draft.to_text) == (kind, "Send the deck", "")
    assert (draft.action_public_id, draft.action_title) == (action.public_id, "Send the deck")
    assert [source.provider_message_id for source in draft.sources] == ["msg-1", "msg-2"]
    await session.refresh(action)
    assert (action.revision, action.status) == (1, "open")  # Drafting never changes it.


async def test_a_reply_for_an_action_answers_its_first_email_still_in_local_mail(
    service: DraftService, session: AsyncSession
) -> None:
    first = await seed_message(session, "msg-1", subject="Deck", sender="gone@example.com")
    second = await seed_message(session, "msg-2", subject="Re: Deck", sender="sam@example.com")
    action = await seed_action(session, [first, second])
    await session.delete(first)
    await session.commit()

    draft = await service.create_for_action(action.public_id, DraftKind.REPLY)

    assert (draft.title, draft.to_text) == ("Re: Deck", "sam@example.com")
    assert [(s.provider_message_id, s.available) for s in draft.sources] == [
        ("msg-1", False),
        ("msg-2", True),
    ]


async def test_a_reply_for_an_action_needs_an_email_in_local_mail(
    service: DraftService, session: AsyncSession
) -> None:
    message = await seed_message(session)
    public_id = (await seed_action(session, [message])).public_id
    await session.delete(message)
    await session.commit()

    with pytest.raises(SourceNotFoundError):
        await service.create_for_action(public_id, DraftKind.REPLY)
    note = await service.create_for_action(public_id, DraftKind.NOTE)
    assert note.sources[0].available is False


async def test_a_draft_for_a_missing_or_deleted_action_is_refused(
    service: DraftService, session: AsyncSession
) -> None:
    public_id = (await seed_action(session, [])).public_id
    with pytest.raises(ActionNotFoundError):
        await service.create_for_action("22222222-2222-4222-8222-222222222222", DraftKind.NOTE)
    await ActionService(session).delete(public_id, 1)
    with pytest.raises(ActionNotFoundError):
        await service.create_for_action(public_id, DraftKind.NOTE)
    assert await count(session, DraftTable) == 0


async def test_autosave_updates_the_draft_without_a_version(
    service: DraftService, clock: Clock, database: Database
) -> None:
    draft = await service.create(DraftKind.EMAIL)
    clock.advance(minutes=1)
    edit = DraftEdit(title=" Hi ", to_text="a@b", cc_text="c@", body="Body\n")

    saved = await service.autosave(draft.public_id, 1, edit)

    assert saved.content() == edit
    assert (saved.revision, saved.updated_at_utc) == (2, clock.now)
    assert (await fresh(database, draft.public_id)).content() == edit
    assert len(await service.versions(draft.public_id)) == 1


async def test_a_stale_autosave_changes_nothing(service: DraftService, database: Database) -> None:
    draft = await service.create(DraftKind.NOTE)
    await service.autosave(draft.public_id, 1, DraftEdit(body="first"))

    with pytest.raises(DraftConflictError) as caught:
        await service.autosave(draft.public_id, 1, DraftEdit(body="private second"))

    assert "private" not in str(caught.value)
    after = await fresh(database, draft.public_id)
    assert (after.body, after.revision) == ("first", 2)


async def test_notes_and_messages_refuse_recipients(service: DraftService) -> None:
    note = await service.create(DraftKind.NOTE)
    with pytest.raises(DraftConflictError, match="recipients"):
        await service.autosave(note.public_id, 1, DraftEdit(to_text="a@b"))
    with pytest.raises(DraftConflictError):
        await service.save_as_new(note.public_id, DraftEdit(cc_text="a@b"))
    assert (await service.get(note.public_id)).revision == 1


async def test_checkpoint_saves_a_version_only_when_the_text_changed(
    service: DraftService, clock: Clock
) -> None:
    draft = await service.create(DraftKind.NOTE)
    assert await service.checkpoint(draft.public_id, 1) is None

    saved = await service.autosave(draft.public_id, 1, DraftEdit(body="Line one\nmore"))
    clock.advance(seconds=5)
    info = await service.checkpoint(draft.public_id, saved.revision)

    assert info is not None
    assert (info.number, info.origin, info.preview, info.length) == (
        2,
        DraftVersionOrigin.EDITED,
        "Line one",
        13,
    )
    assert info.created_at_utc == clock.now
    assert await service.checkpoint(draft.public_id, saved.revision) is None
    assert (await service.get(draft.public_id)).revision == saved.revision  # Unchanged.
    with pytest.raises(DraftConflictError):
        await service.checkpoint(draft.public_id, 1)


async def test_versions_are_pruned_to_the_newest_hundred(
    service: DraftService, session: AsyncSession
) -> None:
    draft = await service.create(DraftKind.NOTE)
    revision = draft.revision
    for index in range(MAX_DRAFT_VERSIONS + 5):
        revision = (
            await service.autosave(draft.public_id, revision, DraftEdit(body=f"v{index}"))
        ).revision
        await service.checkpoint(draft.public_id, revision)

    versions = await service.versions(draft.public_id)

    assert len(versions) == MAX_DRAFT_VERSIONS == 100
    assert [versions[0].number, versions[-1].number] == [106, 7]
    assert await count(session, DraftVersionTable) == 100
    with pytest.raises(DraftNotFoundError, match="version"):
        await service.version(draft.public_id, 6)


async def test_versions_and_restore(service: DraftService, clock: Clock) -> None:
    draft = await service.create(DraftKind.EMAIL)
    first = DraftEdit(title="Plan", to_text="a@b", body="First")
    revision = (await service.autosave(draft.public_id, 1, first)).revision
    await service.checkpoint(draft.public_id, revision)  # Version 2.
    second = DraftEdit(title="Plan", body="Second, unsaved")
    revision = (await service.autosave(draft.public_id, revision, second)).revision
    clock.advance(minutes=1)

    restored = await service.restore_version(draft.public_id, revision, 2)

    assert restored.content() == first
    assert restored.revision == revision + 1
    versions = await service.versions(draft.public_id)
    assert [(v.number, v.origin) for v in versions] == [
        (4, DraftVersionOrigin.RESTORED),
        (3, DraftVersionOrigin.EDITED),
        (2, DraftVersionOrigin.EDITED),
        (1, DraftVersionOrigin.CREATED),
    ]
    assert (await service.version(draft.public_id, 3)).content() == second


async def test_restoring_back_undoes_a_restore(service: DraftService) -> None:
    draft = await service.create(DraftKind.NOTE)
    revision = (await service.autosave(draft.public_id, 1, DraftEdit(body="Later"))).revision

    restored = await service.restore_version(draft.public_id, revision, 1)
    assert restored.body == ""
    back = await service.restore_version(draft.public_id, restored.revision, 2)

    assert back.body == "Later"
    assert [v.origin for v in await service.versions(draft.public_id)] == [
        DraftVersionOrigin.RESTORED,
        DraftVersionOrigin.RESTORED,
        DraftVersionOrigin.EDITED,
        DraftVersionOrigin.CREATED,
    ]


async def test_restoring_the_current_text_changes_nothing(service: DraftService) -> None:
    draft = await service.create(DraftKind.NOTE)

    same = await service.restore_version(draft.public_id, 1, 1)

    assert same.revision == 1
    assert len(await service.versions(draft.public_id)) == 1


async def test_restore_version_refuses_stale_or_missing(service: DraftService) -> None:
    draft = await service.create(DraftKind.NOTE)
    await service.autosave(draft.public_id, 1, DraftEdit(body="x"))
    with pytest.raises(DraftConflictError):
        await service.restore_version(draft.public_id, 1, 1)
    with pytest.raises(DraftNotFoundError):
        await service.restore_version(draft.public_id, 2, 99)
    assert len(await service.versions(draft.public_id)) == 1


async def test_save_as_new_copies_kind_links_and_sources(
    service: DraftService, session: AsyncSession
) -> None:
    message = await seed_message(session)
    action = await seed_action(session, [message])
    original = await service.create_for_action(action.public_id, DraftKind.REPLY)
    await service.delete(original.public_id, 1)  # Deleted elsewhere while being edited.
    edit = DraftEdit(title="Re: Budget review", to_text="alex@example.com", body="Mine")

    copy = await service.save_as_new(original.public_id, edit)

    assert copy.public_id != original.public_id
    assert (copy.kind, copy.revision, copy.content()) == (DraftKind.REPLY, 1, edit)
    assert (copy.action_public_id, copy.action_title) == (action.public_id, "Send the deck")
    assert copy.sources == original.sources
    (version,) = await service.versions(copy.public_id)
    assert (version.origin, version.preview) == (DraftVersionOrigin.CREATED, "Mine")
    with pytest.raises(DraftNotFoundError):
        await service.save_as_new("22222222-2222-4222-8222-222222222222", edit)


async def test_delete_and_restore(service: DraftService, clock: Clock) -> None:
    draft = await service.create(DraftKind.NOTE)
    with pytest.raises(DraftConflictError):
        await service.delete(draft.public_id, 7)

    clock.advance(minutes=1)
    await service.delete(draft.public_id, 1)

    assert await service.list_summaries() == ()
    with pytest.raises(DraftNotFoundError, match="^That draft was not found.$"):
        await service.get(draft.public_id)
    with pytest.raises(DraftNotFoundError):
        await service.versions(draft.public_id)
    restored = await service.restore(draft.public_id)
    assert restored.revision == 3
    assert (await service.restore(draft.public_id)).revision == 3  # Already live.
    assert [s.public_id for s in await service.list_summaries()] == [draft.public_id]
    with pytest.raises(DraftNotFoundError):
        await service.restore("22222222-2222-4222-8222-222222222222")


async def test_the_list_is_newest_first_and_never_capped(
    service: DraftService, session: AsyncSession, clock: Clock
) -> None:
    message = await seed_message(session)
    action = await seed_action(session, [message])
    linked = await service.create_for_action(action.public_id, DraftKind.NOTE)
    for index in range(250):
        clock.advance(seconds=1)
        made = await service.create(DraftKind.MESSAGE)
        await service.autosave(made.public_id, 1, DraftEdit(body=f"Hi [[name]] {index} [[x]]"))
    clock.advance(seconds=1)
    reply = await service.create_reply(OWNER, "msg-1")

    summaries = await service.list_summaries()

    assert len(summaries) == 252
    assert summaries[0].public_id == reply.public_id
    assert (summaries[0].display_title, summaries[0].source_subject) == (
        "Re: Budget review",
        "Budget review",
    )
    assert (
        summaries[1].display_title,
        summaries[1].placeholder_count,
        summaries[1].revision,
    ) == ("Hi [[name]] 249 [[x]]", 2, 2)
    assert summaries[-1].public_id == linked.public_id
    assert (summaries[-1].action_title, summaries[-1].source_subject) == (
        "Send the deck",
        "Budget review",
    )


async def test_drafts_survive_message_account_and_action_deletion(
    service: DraftService, session: AsyncSession
) -> None:
    message = await seed_message(session)
    action = await seed_action(session, [message])
    note = await service.create_for_action(action.public_id, DraftKind.NOTE)
    reply = await service.create_reply(OWNER, "msg-1")

    await ActionService(session).delete(action.public_id, 1)
    assert (await service.get(note.public_id)).action_public_id is None  # Soft-deleted.
    await ActionService(session).restore(action.public_id)
    assert (await service.get(note.public_id)).action_public_id == action.public_id

    row = await session.get(ActionTable, action.id)
    await session.delete(row)
    account = await AccountRepository(session).get_by_email(OWNER)
    assert account is not None
    await AccountRepository(session).delete_by_id(account.id)
    await session.commit()

    kept = await service.get(note.public_id)
    assert (kept.action_public_id, kept.action_title) == (None, "Send the deck")
    assert kept.sources[0].available is False
    assert (kept.sources[0].subject, kept.sources[0].sender_address) == (
        "Budget review",
        "alex@example.com",
    )
    assert (await service.get(reply.public_id)).sources[0].available is False
    assert await count(session, MessageTable) == 0
    assert await count(session, DraftSourceTable) == 2
    assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []


async def test_the_default_clock_and_ids_are_real(session: AsyncSession) -> None:
    before = datetime.now(UTC)

    draft = await DraftService(session).create(DraftKind.NOTE)

    assert before <= draft.created_at_utc <= datetime.now(UTC)
    assert len(draft.public_id) == 36 and draft.public_id.count("-") == 4
