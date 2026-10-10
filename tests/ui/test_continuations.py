"""Thread continuations on the brief, "Add to" an existing action with Undo, Mark seen, and
the thread-check status line."""

import asyncio
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import (
    ActionFilter,
    SuggestionState,
    SuggestionView,
    ThreadActivity,
    ThreadLink,
)
from mailbrief.domain.analysis import ActionOwnership
from mailbrief.domain.briefs import BriefRunResult, BriefStatus
from mailbrief.domain.digests import DailyDigest, DigestStatus, SyncResult, SyncStatus
from mailbrief.services.actions import ActionConflictError, ActionNotFoundError
from mailbrief.ui.main_window import MainWindow, thread_check_text
from tests.factories import fingerprint_of, make_action, make_digest_item, make_suggestion
from tests.ui.brief_view import detail, shown_text
from tests.ui.test_workflow import FakeBackend

KEY = "reply-1"
PENDING = SuggestionView(
    suggestion_id=7,
    state=SuggestionState.PENDING,
    suggestion=make_suggestion(
        position=0, title="Send the figures", fingerprint=fingerprint_of("f")
    ),
)
TRACKED = ThreadLink(
    public_id="22222222-2222-4222-8222-222222222222",
    title='<a href="https://evil.example">Chase</a> the <b>deck</b>',
    revision=3,
    ownership=ActionOwnership.WAITING_FOR,
    is_source=False,
)
OWN = ThreadLink(
    public_id="33333333-3333-4333-8333-333333333333",
    title="Book the room",
    revision=1,
    ownership=ActionOwnership.MINE,
    is_source=True,
)
LINKS: dict[str, tuple[ThreadLink, ...]] = {KEY: (TRACKED, OWN)}


def brief(local_date: date = date(2026, 9, 4), *views: SuggestionView) -> DailyDigest:
    return DailyDigest(
        account_id="owner@example.com",
        local_date=local_date,
        timezone_name="UTC",
        generated_at_utc=datetime(2026, 9, 4, 12, tzinfo=UTC),
        status=DigestStatus.COMPLETE,
        items=(
            make_digest_item(
                message_key=KEY,
                suggestions=views or (PENDING,),
                source_url="https://mail.google.com/mail/u/?authuser=owner%40example.com#all/a",
            ),
        ),
    )


# The window


@pytest.fixture
def backend() -> FakeBackend:
    result = FakeBackend()
    result.saved = brief()
    result.links = LINKS
    return result


@pytest.fixture
def window(qtbot: QtBot, backend: FakeBackend) -> MainWindow:
    result = MainWindow(backend)
    result.zone = ZoneInfo("UTC")
    result.now = lambda: datetime(2026, 9, 5, 15, tzinfo=UTC)
    qtbot.addWidget(result)
    return result


async def finish(window: MainWindow) -> None:
    assert window.task is not None
    await window.task


async def test_every_brief_is_shown_with_its_continuations(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()

    assert backend.link_calls == [backend.saved]
    assert "Continues: “" in shown_text(window)


async def test_a_brief_whose_links_fail_to_load_is_shown_without_them(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.links_fail = RuntimeError("private detail")
    await window.initialize()

    assert "Approval needed by Friday" in shown_text(window)
    assert "Continues" not in shown_text(window)
    assert "private detail" not in window.status.text()


@pytest.mark.parametrize("source_added", [True, False])
async def test_add_to_then_undo(
    window: MainWindow, backend: FakeBackend, source_added: bool
) -> None:
    await window.initialize()
    backend.source_added = source_added
    loads = backend.loads

    detail(window).accept_into_requested.emit(7, TRACKED.public_id, TRACKED.revision)
    await finish(window)

    assert backend.action_calls == [("accept_into", 7, TRACKED.public_id, 3)]
    assert window.status.text() == "Added to: Send the deck."
    assert backend.loads == loads + 1
    assert window.undo_button.text() == "&Undo add"

    window.undo_button.click()
    await finish(window)

    assert backend.action_calls[-1] == ("undo_accept_into", 7, TRACKED.public_id, 4, source_added)
    assert backend.undo_previous is not None  # What accept_into replaced goes back.
    assert backend.undo_previous.decision is SuggestionState.DISMISSED
    assert window.status.text() == "Undone."
    assert window.undo_button.isHidden()


async def test_repeating_add_to_offers_no_undo(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()
    backend.add_changed = False  # The suggestion was already added to that action.

    detail(window).accept_into_requested.emit(7, TRACKED.public_id, TRACKED.revision)
    await finish(window)

    assert backend.action_calls == [("accept_into", 7, TRACKED.public_id, 3)]
    assert window.status.text() == "Already added to: Send the deck."
    assert window.undo_button.isHidden()


async def test_an_add_to_conflict_shows_its_message_and_refreshes(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.action_fail = ActionConflictError("That suggestion already belongs to another action.")
    loads, lists = backend.loads, backend.list_calls

    detail(window).accept_into_requested.emit(7, TRACKED.public_id, TRACKED.revision)
    await finish(window)

    assert window.status.text() == "That suggestion already belongs to another action."
    assert (backend.loads, backend.list_calls) == (loads + 1, lists + 3)
    assert window.undo_button.isHidden()


async def test_add_to_a_deleted_action_reloads(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()
    backend.action_fail = ActionNotFoundError("That action was not found.")

    detail(window).accept_into_requested.emit(7, OWN.public_id, OWN.revision)
    await finish(window)

    assert "no longer available" in window.status.text()
    assert window.undo_button.isHidden()


async def test_a_refused_undo_add_explains_itself(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()
    detail(window).accept_into_requested.emit(7, TRACKED.public_id, TRACKED.revision)
    await finish(window)
    backend.action_fail = ActionConflictError("Only an unchanged addition can be undone.")

    window.undo_button.click()
    await finish(window)

    assert window.status.text() == "Can't undo: it has changed since then."


async def test_add_to_and_undo_stay_on_a_past_brief(
    window: MainWindow, backend: FakeBackend
) -> None:
    past = brief(date(2026, 9, 3))
    backend.briefs[(past.account_id, past.local_date)] = past
    await window.initialize()

    window.start(lambda: window._show_brief(past.account_id, past.local_date))
    await finish(window)
    assert backend.link_calls[-1] == past
    assert "Continues: “" in shown_text(window)

    detail(window).accept_into_requested.emit(7, TRACKED.public_id, TRACKED.revision)
    await finish(window)
    assert window.viewing_label.text() == "Viewing the brief for Thu Sep 3."
    assert shown_text(window).startswith("Thu Sep 3")
    assert backend.link_calls[-1] == past

    window.undo_button.click()
    await finish(window)
    assert window.viewing_label.text() == "Viewing the brief for Thu Sep 3."
    assert shown_text(window).startswith("Thu Sep 3")


async def test_mark_seen_runs_for_an_action_with_activity(
    window: MainWindow, backend: FakeBackend
) -> None:
    active = make_action(
        title="Chase Sam",
        revision=5,
        thread=ThreadActivity(
            new_messages=1,
            latest_at_utc=datetime(2026, 9, 5, 9, tzinfo=UTC),
            latest_sender="Sam",
        ),
    )
    backend.actions = {ActionFilter.OPEN: (active,)}
    await window.initialize()
    lists = backend.list_calls

    assert window.actions_panel.seen_button.isEnabled()
    window.actions_panel.seen_button.click()
    await finish(window)

    assert backend.action_calls == [("mark_thread_seen", active.public_id, 5)]
    assert window.status.text() == "Marked seen: Approve the proposal."
    assert backend.list_calls == lists + 3


async def test_mark_seen_on_a_changed_action_reloads(
    window: MainWindow, backend: FakeBackend
) -> None:
    active = make_action(
        thread=ThreadActivity(owner_replied_at_utc=datetime(2026, 9, 5, tzinfo=UTC))
    )
    backend.actions = {ActionFilter.OPEN: (active,)}
    await window.initialize()
    backend.action_fail = ActionConflictError("changed")

    window.actions_panel.seen_button.click()
    await finish(window)

    assert window.status.text() == "That changed or is no longer available; the view was reloaded."


def sync(**threads: object) -> SyncResult:
    return SyncResult(
        account_id="owner@example.com",
        range_start_utc=datetime(2026, 9, 5, tzinfo=UTC),
        range_end_utc=datetime(2026, 9, 6, tzinfo=UTC),
        status=SyncStatus.COMPLETE,
        page_count=1,
        message_count=1,
        **threads,
    )


@pytest.mark.parametrize(
    ("threads", "sentence"),
    [
        ({}, ""),
        ({"threads_tracked": 3, "threads_checked": 3}, "Checked 3 tracked threads."),
        ({"threads_tracked": 1, "threads_checked": 1}, "Checked 1 tracked thread."),
        (
            {"threads_tracked": 5, "threads_checked": 3, "threads_failed": 2},
            "Checked 3 of 5 tracked threads; 2 failed.",
        ),
        (
            {"threads_tracked": 5, "threads_checked": 1, "threads_stopped_code": "AUTH_REQUIRED"},
            "Thread checks stopped early.",
        ),
        ({"threads_stopped_code": "THREAD_CHECK_FAILED"}, "Thread checks stopped early."),
    ],
)
def test_the_thread_check_reads_as_one_sentence_without_codes(
    threads: dict[str, object], sentence: str
) -> None:
    assert thread_check_text(sync(**threads)) == sentence


async def test_a_brief_run_reports_its_thread_check(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.sync = sync(threads_tracked=4, threads_checked=3, threads_failed=1)
    await window.initialize()

    window.start(window._generate)
    for _ in range(3):
        await asyncio.sleep(0)
    window.review_button.click()
    for _ in range(3):
        await asyncio.sleep(0)
    window.approve_button.click()
    await finish(window)

    assert window.status.text() == (
        "Brief saved (Complete). Checked 3 of 4 tracked threads; 1 failed."
    )
    assert backend.link_calls[-1] == backend.saved


async def test_a_cancelled_run_says_nothing_about_threads(
    window: MainWindow, backend: FakeBackend
) -> None:
    backend.sync = sync(threads_tracked=5, threads_checked=2)
    await window.initialize()

    async def cancelled(*_args: object, **_kwargs: object) -> BriefRunResult:
        return BriefRunResult(status=BriefStatus.CANCELLED, sync=backend.sync)

    backend.generate = cancelled  # type: ignore[method-assign]
    window.start(window._generate)
    await finish(window)

    assert window.status.text() == "Cancelled. The displayed saved brief is unchanged."
