"""Two writers holding the same revision must not both succeed.

The services compare the caller's revision when they read the row, but two sessions can
both pass that check before either writes: inside the desktop app DraftWrites serializes
writers, while the diagnostic CLI opens the same database without taking desktop.lock.
The UPDATE itself therefore carries the revision (version_id_col), and the second writer
gets a conflict. This interleaves two autosaves deterministically: both read revision N,
then both write.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mailbrief.domain.actions import ProposalState
from mailbrief.domain.drafts import DraftEdit, DraftKind
from mailbrief.services.actions import ActionConflictError, ActionService
from mailbrief.services.drafts import DraftConflictError, DraftService
from mailbrief.services.proposals import ProposalService
from mailbrief.storage.database import Database
from mailbrief.storage.migrate import upgrade_database
from mailbrief.storage.tables import ActionProposalTable, ActionTable, DraftTable

_NOW = datetime(2026, 10, 3, 9, tzinfo=UTC)
_PUBLIC_ID = "00000000-0000-4000-8000-000000000001"


async def _migrated(tmp_path: Path) -> Database:
    path = tmp_path / "mailbrief.sqlite3"
    await asyncio.to_thread(upgrade_database, path)
    return Database.from_path(path)


async def _seed_action(database: Database) -> None:
    async with database.transaction() as session:
        session.add(
            ActionTable(
                public_id=_PUBLIC_ID,
                title="Send the report",
                ownership="mine",
                status="open",
                created_at_utc=_NOW,
                updated_at_utc=_NOW,
                revision=1,
            )
        )


async def test_stale_writer_is_rejected_not_silently_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "mailbrief.sqlite3"
    # The real schema, WAL mode and pragmas, as both processes see it. Alembic's env.py
    # calls asyncio.run(), so migrate in a worker thread, exactly as DesktopRuntime does.
    await asyncio.to_thread(upgrade_database, path)
    database = Database.from_path(path)
    try:
        async with database.session() as session:
            draft = await DraftService(session).create(DraftKind.NOTE)

        both_have_read = asyncio.Barrier(2)
        original_live = DraftService._live

        async def live_then_wait(
            self: DraftService, public_id: str, expected_revision: int | None
        ) -> DraftTable:
            row = await original_live(self, public_id, expected_revision)
            await both_have_read.wait()  # Each writer has checked the revision; now both write.
            return row

        monkeypatch.setattr(DraftService, "_live", live_then_wait)

        async def autosave(body: str) -> object:
            async with database.session() as session:
                return await DraftService(session).autosave(
                    draft.public_id, draft.revision, DraftEdit(body=body)
                )

        results = await asyncio.gather(
            autosave("Written in the desktop app"),
            autosave("Written from the CLI"),
            return_exceptions=True,
        )
        monkeypatch.undo()
        async with database.session() as session:
            final = await DraftService(session).get(draft.public_id)

        conflicts = sum(isinstance(result, DraftConflictError) for result in results)
        assert conflicts == 1, (
            f"both writers succeeded from revision {draft.revision}; "
            f"final revision {final.revision} kept {final.body!r}"
        )
    finally:
        await database.dispose()


async def test_stale_action_writer_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two processes complete the same action from revision 1: exactly one wins."""
    database = await _migrated(tmp_path)
    try:
        await _seed_action(database)
        both_have_read = asyncio.Barrier(2)
        original_live = ActionService._live

        async def live_then_wait(
            self: ActionService, public_id: str, expected_revision: int | None
        ) -> ActionTable:
            row = await original_live(self, public_id, expected_revision)
            await both_have_read.wait()
            return row

        monkeypatch.setattr(ActionService, "_live", live_then_wait)

        async def complete() -> object:
            async with database.session() as session:
                return await ActionService(session).complete(_PUBLIC_ID, 1)

        results = await asyncio.gather(complete(), complete(), return_exceptions=True)
        assert sum(isinstance(result, ActionConflictError) for result in results) == 1, results
    finally:
        await database.dispose()


async def test_stale_proposal_apply_is_a_conflict_and_leaves_it_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proposal applied over an action another writer changed after it was loaded
    fails with the service's conflict error, and the proposal stays pending."""
    database = await _migrated(tmp_path)
    try:
        await _seed_action(database)
        async with database.transaction() as session:
            action = await session.get(ActionTable, 1)
            assert action is not None
            session.add(
                ActionProposalTable(
                    action_id=action.id,
                    kind="cancelled",
                    state=ProposalState.PENDING.value,
                    evidence="The order is cancelled.",
                    provider="gmail",
                    provider_account_id="account-1",
                    provider_message_id="message-1",
                    subject="Order",
                    sender_address="shop@example.com",
                    web_link="https://mail.google.com/mail/u/0/#all/message-1",
                    received_at_utc=_NOW,
                    created_at_utc=_NOW,
                )
            )

        loaded = asyncio.Event()
        resume = asyncio.Event()
        original_action = ProposalService._action

        async def action_then_wait(self: ProposalService, row: ActionProposalTable) -> ActionTable:
            found = await original_action(self, row)
            loaded.set()
            await resume.wait()
            return found

        monkeypatch.setattr(ProposalService, "_action", action_then_wait)

        async def apply() -> object:
            async with database.session() as session:
                return await ProposalService(session).apply(1, 1)

        task = asyncio.create_task(apply())
        await loaded.wait()
        async with database.session() as session:
            await ActionService(session).complete(_PUBLIC_ID, 1)
        resume.set()
        with pytest.raises(ActionConflictError):
            await task

        monkeypatch.undo()
        async with database.session() as session:
            proposal = await session.get(ActionProposalTable, 1)
            assert proposal is not None
            assert proposal.state == ProposalState.PENDING.value
    finally:
        await database.dispose()
