"""The desktop runtime's draft calls against a real, migrated database, with no network."""

from pathlib import Path
from typing import Any, NoReturn

import pytest

from mailbrief.domain.actions import ActionFilter
from mailbrief.domain.drafts import DraftEdit, DraftKind, DraftVersionOrigin
from mailbrief.services.drafts import DraftConflictError, SourceNotFoundError
from mailbrief.ui import runtime as runtime_module
from mailbrief.ui.runtime import DesktopRuntime
from tests.unit.test_desktop_actions import seed_brief


def refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise AssertionError("Drafting must never open Gmail or Groq.")


@pytest.fixture(autouse=True)
def no_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every drafts path must work with the provider factories unusable."""
    monkeypatch.setattr(runtime_module, "gmail_provider", refuse)
    monkeypatch.setattr(runtime_module, "groq_provider", refuse)


async def test_every_draft_call_works_offline_on_local_storage(tmp_path: Path) -> None:
    path = tmp_path / "mailbrief.sqlite3"
    runtime = DesktopRuntime(path)
    try:
        assert await runtime.load_saved() is None  # Migrates the new database.
        await seed_brief(path)
        saved = await runtime.load_saved()
        assert saved is not None
        item = saved.items[0]

        reply = await runtime.create_reply_draft(saved.account_id, item.message_key)
        assert (reply.title, reply.to_text) == ("Re: Approval needed by Friday", "alex@example.com")
        assert reply.sources[0].available
        with pytest.raises(SourceNotFoundError):
            await runtime.create_reply_draft(saved.account_id, "not-cached")

        action = await runtime.accept_suggestion(item.suggestions[0].suggestion_id)
        note = await runtime.create_draft_for_action(action.public_id, DraftKind.NOTE)
        assert (note.title, note.action_public_id) == ("Approve the budget", action.public_id)
        blank = await runtime.create_draft(DraftKind.MESSAGE)

        edit = DraftEdit(body="On my way [[time]]")
        typed = await runtime.autosave_draft(blank.public_id, 1, edit)
        with pytest.raises(DraftConflictError):
            await runtime.autosave_draft(blank.public_id, 1, edit)
        info = await runtime.checkpoint_draft(blank.public_id, typed.revision)
        assert info is not None and info.number == 2
        versions = await runtime.draft_versions(blank.public_id)
        assert [v.origin for v in versions] == [
            DraftVersionOrigin.EDITED,
            DraftVersionOrigin.CREATED,
        ]
        assert (await runtime.draft_version(blank.public_id, 2)).body == "On my way [[time]]"
        restored = await runtime.restore_draft_version(blank.public_id, typed.revision, 1)
        assert restored.body == ""
        copy = await runtime.save_draft_as_new(blank.public_id, edit)
        assert (await runtime.get_draft(copy.public_id)).body == edit.body

        await runtime.delete_draft(copy.public_id, copy.revision)
        listed = await runtime.list_drafts()
        assert copy.public_id not in [summary.public_id for summary in listed]
        assert len(listed) == 3
        await runtime.restore_draft(copy.public_id)
        assert len(await runtime.list_drafts()) == 4
        # Drafting never changes the action.
        assert (await runtime.list_actions(ActionFilter.OPEN))[0].revision == action.revision
    finally:
        await runtime.close()


async def test_draft_calls_need_initialized_storage(tmp_path: Path) -> None:
    runtime = DesktopRuntime(tmp_path / "unopened.sqlite3")

    with pytest.raises(RuntimeError):
        await runtime.list_drafts()
    with pytest.raises(RuntimeError):
        await runtime.create_draft(DraftKind.NOTE)
