"""The desktop runtime's action calls against a real, migrated database."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from mailbrief.domain.actions import (
    ActionEdit,
    ActionFilter,
    ActionStatus,
    StepEdit,
    SuggestionState,
)
from mailbrief.domain.analysis import ActionOwnership, ActionSuggestion
from mailbrief.domain.briefs import AnalysisOutcome
from mailbrief.domain.digests import DailyDigest, DigestSection, DigestStatus
from mailbrief.domain.messages import AccountIdentity, ProviderKind, RankedMessage
from mailbrief.services.actions import ActionConflictError, DecisionSnapshot
from mailbrief.services.analysis import PlannedMessage, suggestion_fingerprint
from mailbrief.services.proposals import ProposalNotFoundError, ProposalService
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


async def test_the_runtime_links_briefs_adds_to_actions_and_marks_threads_seen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mailbrief.sqlite3"
    runtime = DesktopRuntime(path)
    try:
        assert await runtime.load_saved() is None  # Migrates the new database.
        await seed_brief(path)
        saved = await runtime.load_saved()
        assert saved is not None
        first, second = (view.suggestion_id for view in saved.items[0].suggestions)
        assert await runtime.brief_links(saved) == {}

        action = await runtime.accept_suggestion(first)
        (link,) = (await runtime.brief_links(saved))[saved.items[0].message_key]
        assert (link.public_id, link.revision, link.is_source) == (action.public_id, 1, True)

        added = await runtime.accept_into(second, action.public_id, 1)
        assert (added.action.revision, added.source_added) == (2, False)
        assert states(await runtime.load_saved()) == [SuggestionState.ACCEPTED] * 2
        assert added.changed and added.previous == DecisionSnapshot()
        undone = await runtime.undo_accept_into(
            second, action.public_id, 2, added.source_added, previous=added.previous
        )
        assert undone.revision == 3
        assert states(await runtime.load_saved()) == [
            SuggestionState.ACCEPTED,
            SuggestionState.PENDING,
        ]

        # Nothing later in its thread: marking it seen makes no new revision.
        assert (await runtime.mark_thread_seen(action.public_id, 3)).revision == 3
        with pytest.raises(ActionConflictError):
            await runtime.mark_thread_seen(action.public_id, 1)
    finally:
        await runtime.close()


async def test_action_calls_need_initialized_storage(tmp_path: Path) -> None:
    runtime = DesktopRuntime(tmp_path / "unopened.sqlite3")

    with pytest.raises(RuntimeError):
        await runtime.list_actions(ActionFilter.OPEN)
    with pytest.raises(RuntimeError):
        await runtime.accept_suggestion(1)


async def seed_reply_brief(path: Path) -> None:
    """A saved brief of 2026-09-29 whose one item is a later reply, in the thread of the
    accepted action's source, that says the work was cancelled."""
    database = Database.from_path(path)
    try:
        async with database.session() as session:
            account = await AccountRepository(session).get_by_email("me@x.com")
            assert account is not None
            reply = make_message(
                provider=ProviderKind.GMAIL,
                provider_account_id="me@x.com",
                provider_message_id="reply-1",
                received_at_utc=datetime(2026, 9, 29, 13, tzinfo=UTC),
                web_link="https://mail.google.com/mail/u/?authuser=me#all/reply-1",
            )
            (row,) = await MessageRepository(session).upsert_messages(account.id, [reply])
            analysis = await AnalysisRepository(session).upsert_analysis(
                message_id=row.id,
                input_hash="reply-hash",
                provider="groq",
                model="model",
                prompt_version="prompt",
                schema_version="7",
                analysis=make_analysis(),
            )
            await DigestRepository(session).save_digest(
                account_id=account.id,
                local_date=date(2026, 9, 29),
                timezone_name="UTC",
                status=DigestStatus.COMPLETE,
                items=[(row.id, analysis.id, 0, DigestSection.FOLLOW_UPS)],
            )
            cancelled = make_analysis(
                category="information",
                action_required=False,
                action_text=None,
                deadline_text=None,
                deadline_precision="none",
                deadline_date=None,
                deadline_at_utc=None,
                deadline_timezone=None,
                follow_up="cancelled",
                follow_up_evidence="Please approve the attached proposal",
            )
            planned = PlannedMessage(
                ranked=RankedMessage(message=reply, score=20),
                message_row_id=row.id,
                request=None,
                input_hash=None,
                outcome=AnalysisOutcome.ANALYZED,
                analysis=cancelled,
            )
            assert await ProposalService(session).derive(account, [planned], "UTC") == 1
    finally:
        await database.dispose()


async def test_the_runtime_lists_applies_undoes_dismisses_and_restores_proposals(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mailbrief.sqlite3"
    runtime = DesktopRuntime(path)
    try:
        assert await runtime.load_saved() is None  # Migrates the new database.
        await seed_brief(path)
        first = await runtime.load_saved()
        assert first is not None
        action = await runtime.accept_suggestion(first.items[0].suggestions[0].suggestion_id)
        await seed_reply_brief(path)
        brief = await runtime.load_brief("me@x.com", date(2026, 9, 29))
        assert brief is not None and brief.items[0].section is DigestSection.FOLLOW_UPS

        # Listed by the email that proposes, with the action's revision as loaded.
        assert await runtime.brief_proposals(first) == {}
        (proposal,) = (await runtime.brief_proposals(brief))["reply-1"]
        assert (proposal.action_public_id, proposal.action_revision) == (action.public_id, 1)
        assert [p.id for p in (await runtime.list_actions(ActionFilter.OPEN))[0].proposals] == [
            proposal.id
        ]

        # A stale revision changes nothing; the current one applies, as one revision.
        with pytest.raises(ActionConflictError):
            await runtime.apply_proposal(proposal.id, 2)
        applied = await runtime.apply_proposal(proposal.id, 1)
        assert (applied.revision, applied.status) == (2, ActionStatus.COMPLETED)
        assert await runtime.brief_proposals(brief) == {}  # Applied, and its action closed.
        assert await runtime.list_actions(ActionFilter.COMPLETED) == (applied,)

        undone = await runtime.undo_apply_proposal(proposal.id, 2)
        assert (undone.revision, undone.status) == (3, ActionStatus.OPEN)
        (again,) = (await runtime.brief_proposals(brief))["reply-1"]
        assert (again.id, again.action_revision) == (proposal.id, 3)
        with pytest.raises(ActionConflictError):
            await runtime.undo_apply_proposal(proposal.id, 3)  # Nothing is applied now.

        await runtime.dismiss_proposal(proposal.id)
        assert await runtime.brief_proposals(brief) == {}
        assert (await runtime.list_actions(ActionFilter.OPEN))[0].proposals == ()
        await runtime.restore_proposal(proposal.id)
        assert list(await runtime.brief_proposals(brief)) == ["reply-1"]

        with pytest.raises(ProposalNotFoundError):
            await runtime.apply_proposal(404, 1)
        with pytest.raises(ProposalNotFoundError):
            await runtime.dismiss_proposal(404)
        with pytest.raises(ProposalNotFoundError):
            await runtime.restore_proposal(404)
    finally:
        await runtime.close()


async def test_proposal_calls_need_initialized_storage(tmp_path: Path) -> None:
    runtime = DesktopRuntime(tmp_path / "unopened.sqlite3")
    digest = DailyDigest(
        account_id="me@x.com",
        local_date=date(2026, 9, 29),
        timezone_name="UTC",
        generated_at_utc=datetime(2026, 9, 29, 12, tzinfo=UTC),
        status=DigestStatus.EMPTY,
    )

    with pytest.raises(RuntimeError):
        await runtime.brief_proposals(digest)
    for call in (runtime.apply_proposal(1, 1), runtime.undo_apply_proposal(1, 1)):
        with pytest.raises(RuntimeError):
            await call
    for simple in (runtime.dismiss_proposal(1), runtime.restore_proposal(1)):
        with pytest.raises(RuntimeError):
            await simple
