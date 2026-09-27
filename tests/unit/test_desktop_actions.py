"""The desktop runtime's action calls against a real, migrated database."""

from datetime import date
from pathlib import Path

import pytest

from mailbrief.domain.actions import ActionEdit, ActionFilter, StepEdit, SuggestionState
from mailbrief.domain.analysis import ActionOwnership, ActionSuggestion
from mailbrief.domain.digests import DailyDigest, DigestSection, DigestStatus
from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.services.actions import ActionConflictError
from mailbrief.services.analysis import suggestion_fingerprint
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import (
    AccountRepository,
    AnalysisRepository,
    DigestRepository,
    MessageRepository,
)
from mailbrief.ui.runtime import DesktopRuntime
from tests.factories import make_analysis, make_message, make_suggestion


def suggestion(position: int, title: str) -> ActionSuggestion:
    return make_suggestion(
        position=position, title=title, fingerprint=suggestion_fingerprint(title)
    )


async def seed_brief(path: Path) -> None:
    """A saved Gmail brief whose one item has two suggestions."""
    database = Database.from_path(path)
    try:
        async with database.session() as session:
            account = await AccountRepository(session).upsert(
                AccountIdentity(
                    provider=ProviderKind.GMAIL,
                    provider_account_id="me@x.com",
                    email_address="me@x.com",
                )
            )
            (message,) = await MessageRepository(session).upsert_messages(
                account.id,
                [make_message(web_link="https://mail.google.com/mail/u/?authuser=me#all/t1")],
            )
            analysis = await AnalysisRepository(session).upsert_analysis(
                message_id=message.id,
                input_hash="hash",
                provider="groq",
                model="model",
                prompt_version="prompt",
                schema_version="6",
                analysis=make_analysis(
                    suggestions=(suggestion(0, "Approve the budget"), suggestion(1, "Book it"))
                ),
            )
            await DigestRepository(session).save_digest(
                account_id=account.id,
                local_date=date(2026, 9, 28),
                timezone_name="UTC",
                status=DigestStatus.COMPLETE,
                items=[(message.id, analysis.id, 0, DigestSection.ACTIONS)],
            )
            await session.commit()
    finally:
        await database.dispose()


def states(digest: DailyDigest | None) -> list[SuggestionState]:
    assert digest is not None
    return [view.state for view in digest.items[0].suggestions]


async def test_the_runtime_accepts_edits_completes_and_restores_actions(tmp_path: Path) -> None:
    path = tmp_path / "mailbrief.sqlite3"
    runtime = DesktopRuntime(path)
    try:
        assert await runtime.load_saved() is None  # Migrates the new database.
        await seed_brief(path)
        saved = await runtime.load_saved()
        assert states(saved) == [SuggestionState.PENDING, SuggestionState.PENDING]
        assert saved is not None
        first, second = (view.suggestion_id for view in saved.items[0].suggestions)

        await runtime.dismiss_suggestion(first)
        assert states(await runtime.load_saved())[0] is SuggestionState.DISMISSED
        await runtime.restore_suggestion(first)
        assert states(await runtime.load_saved())[0] is SuggestionState.PENDING

        action = await runtime.accept_suggestion(first)
        assert states(await runtime.load_saved())[0] is SuggestionState.ACCEPTED
        assert await runtime.list_actions(ActionFilter.OPEN) == (action,)

        saved_edit = await runtime.save_action(
            action.public_id,
            1,
            ActionEdit(
                title="Approve the final budget",
                ownership=ActionOwnership.MINE,
                effort=None,
                target_date=date(2026, 10, 1),
                notes="Ask Sam first.",
            ),
            [StepEdit(step_id=None, text="Check the totals", done=True)],
        )
        assert saved_edit.revision == 2  # The edit and the new plan are one revision.
        assert [(step.text, step.done) for step in saved_edit.steps] == [("Check the totals", True)]

        completed = await runtime.complete_action(action.public_id, 2)
        assert await runtime.list_actions(ActionFilter.COMPLETED) == (completed,)
        reopened = await runtime.reopen_action(action.public_id, 3)
        await runtime.delete_action(action.public_id, reopened.revision)
        assert await runtime.list_actions(ActionFilter.OPEN) == ()
        assert states(await runtime.load_saved())[0] is SuggestionState.DISMISSED
        restored = await runtime.restore_action(action.public_id)
        assert restored.revision == 6
        assert await runtime.list_actions(ActionFilter.OPEN) == (restored,)

        undoable = await runtime.accept_suggestion(second)
        with pytest.raises(ActionConflictError):
            await runtime.unaccept_action(undoable.public_id, 2)
        await runtime.unaccept_action(undoable.public_id, 1)
        assert states(await runtime.load_saved())[1] is SuggestionState.PENDING
    finally:
        await runtime.close()


async def test_saving_an_edit_without_a_plan_leaves_the_steps_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "mailbrief.sqlite3"
    runtime = DesktopRuntime(path)
    try:
        assert await runtime.load_saved() is None  # Migrates the new database.
        await seed_brief(path)
        saved = await runtime.load_saved()
        assert saved is not None
        action = await runtime.accept_suggestion(saved.items[0].suggestions[0].suggestion_id)
        edit = ActionEdit(
            title="Approve the final budget",
            ownership=ActionOwnership.MINE,
            effort=None,
            target_date=None,
            notes="",
        )
        planned = await runtime.save_action(
            action.public_id,
            1,
            edit,
            [StepEdit(step_id=action.steps[0].step_id, text="Read it twice", done=True)],
        )

        kept = await runtime.save_action(
            action.public_id, 2, edit.model_copy(update={"notes": "Ask Sam."}), steps=None
        )

        assert (kept.revision, kept.notes) == (3, "Ask Sam.")
        assert kept.steps == planned.steps
        assert [(step.text, step.done) for step in kept.steps] == [("Read it twice", True)]
    finally:
        await runtime.close()


async def test_action_calls_need_initialized_storage(tmp_path: Path) -> None:
    runtime = DesktopRuntime(tmp_path / "unopened.sqlite3")

    with pytest.raises(RuntimeError):
        await runtime.list_actions(ActionFilter.OPEN)
    with pytest.raises(RuntimeError):
        await runtime.accept_suggestion(1)
