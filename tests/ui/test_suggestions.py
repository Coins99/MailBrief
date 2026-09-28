"""Suggestions on the brief: escaped rendering, link routing, accept, dismiss and undo."""

import asyncio
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest
from PySide6.QtCore import Qt, QUrl
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import SuggestionState, SuggestionView
from mailbrief.domain.analysis import ActionOwnership, DeadlinePrecision, TargetReason
from mailbrief.domain.digests import DailyDigest, DigestStatus
from mailbrief.services.actions import ActionConflictError, SuggestionNotFoundError
from mailbrief.ui.digest_view import ACCEPT, DISMISS, DigestView
from mailbrief.ui.main_window import MainWindow
from tests.factories import fingerprint_of, make_digest_item, make_suggestion
from tests.ui.test_workflow import FakeBackend

PENDING = SuggestionView(
    suggestion_id=7,
    state=SuggestionState.PENDING,
    suggestion=make_suggestion(
        position=0,
        title='<a href="https://evil.example">Approve</a> the budget',
        fingerprint=fingerprint_of("approve"),
        deadline_text="by Friday",
        deadline_precision=DeadlinePrecision.DATE,
        deadline_date=date(2026, 10, 2),
        deadline_timezone="UTC",
        suggested_target_date=date(2026, 10, 1),
        target_reason=TargetReason.WORKING_DAY_BEFORE,
        steps=("Check the <b>totals</b>", "Reply to finance"),
    ),
)
ACCEPTED = SuggestionView(
    suggestion_id=8,
    state=SuggestionState.ACCEPTED,
    action_public_id="0c5e2c1d-6b8e-4f55-9d0e-2a7f3b9c1e44",
    suggestion=make_suggestion(position=1, title="Book the room", fingerprint=fingerprint_of("b")),
)
DISMISSED = SuggestionView(
    suggestion_id=9,
    state=SuggestionState.DISMISSED,
    suggestion=make_suggestion(
        position=2,
        title="Hidden after dismissal",
        fingerprint=fingerprint_of("c"),
        ownership=ActionOwnership.WAITING_FOR,
    ),
)


def brief(*views: SuggestionView) -> DailyDigest:
    return DailyDigest(
        account_id="owner@example.com",
        local_date=date(2026, 9, 28),
        timezone_name="UTC",
        generated_at_utc=datetime(2026, 9, 28, 12, tzinfo=UTC),
        status=DigestStatus.COMPLETE,
        items=(
            make_digest_item(
                suggestions=views,
                source_url="https://mail.google.com/mail/u/?authuser=owner%40example.com#all/a",
            ),
        ),
    )


@pytest.fixture
def backend() -> FakeBackend:
    result = FakeBackend()
    result.saved = brief(PENDING, ACCEPTED, DISMISSED)
    return result


@pytest.fixture
def window(qtbot: QtBot, backend: FakeBackend) -> MainWindow:
    result = MainWindow(backend)
    qtbot.addWidget(result)
    return result


async def finish(window: MainWindow) -> None:
    assert window.task is not None
    await window.task


def test_suggestions_render_escaped_with_their_plan_and_target(qtbot: QtBot) -> None:
    view = DigestView()
    qtbot.addWidget(view)

    view.show_digest(brief(PENDING, ACCEPTED, DISMISSED))

    text = view.toPlainText()
    assert 'Suggested: <a href="https://evil.example">Approve</a> the budget' in text
    assert "target 2026-10-01, one working day before the deadline" in text
    assert "due 2026-10-02" in text
    assert "Check the <b>totals</b>" in text
    assert "Accepted: Book the room" in text
    assert "Hidden after dismissal" not in text
    # The title's markup is text: a real anchor would start with a raw "<a ".
    assert '<a href="https://evil.example"' not in view.toHtml()


def test_only_the_brief_s_own_suggestion_links_are_acted_on(qtbot: QtBot) -> None:
    view = DigestView()
    qtbot.addWidget(view)
    view.show_digest(brief(PENDING, ACCEPTED, DISMISSED))
    requests: list[tuple[str, int]] = []
    view.suggestion_requested.connect(lambda kind, key: requests.append((kind, key)))

    for link in (
        "mailbrief:accept/7",
        "mailbrief:dismiss/7",
        "mailbrief:accept/8",  # Already accepted: no link was drawn for it.
        "mailbrief:dismiss/9",  # Dismissed: hidden.
        "mailbrief:accept/999",
        "https://evil.example",
    ):
        view.anchorClicked.emit(QUrl(link))

    assert requests == [(ACCEPT, 7), (DISMISS, 7)]

    view.show_digest(brief())  # A new brief forgets the old links.
    view.anchorClicked.emit(QUrl("mailbrief:accept/7"))
    assert requests == [(ACCEPT, 7), (DISMISS, 7)]


def test_a_suggestion_without_dates_names_only_its_owner(qtbot: QtBot) -> None:
    view = DigestView()
    qtbot.addWidget(view)
    undated = SuggestionView(
        suggestion_id=10,
        state=SuggestionState.PENDING,
        suggestion=make_suggestion(
            position=0, title="Book the room", fingerprint=fingerprint_of("room"), steps=()
        ),
    )

    view.show_digest(brief(undated))

    (line,) = [line for line in view.toPlainText().splitlines() if line.startswith("Suggested:")]
    assert line == "Suggested: Book the room (yours)"  # No "target" or "due" part.


def test_an_exact_suggestion_deadline_reads_in_the_brief_s_zone(qtbot: QtBot) -> None:
    view = DigestView()
    qtbot.addWidget(view)
    friday_night = SuggestionView(
        suggestion_id=11,
        state=SuggestionState.PENDING,
        suggestion=make_suggestion(
            position=0,
            title="Send the contract",
            fingerprint=fingerprint_of("contract"),
            steps=(),
            deadline_text="Friday 11 PM Pacific",
            deadline_precision=DeadlinePrecision.DATETIME,
            deadline_date=date(2026, 10, 2),
            deadline_at_utc=datetime(2026, 10, 3, 6, 0, tzinfo=UTC),
            deadline_timezone="America/Los_Angeles",
        ),
    )

    view.show_digest(brief(friday_night).model_copy(update={"timezone_name": "America/Toronto"}))

    (line,) = [line for line in view.toPlainText().splitlines() if line.startswith("Suggested:")]
    assert line == "Suggested: Send the contract (yours; due 2026-10-03 02:00)"  # Saturday.


def test_an_unresolved_suggestion_deadline_shows_its_words(qtbot: QtBot) -> None:
    view = DigestView()
    qtbot.addWidget(view)
    soon = SuggestionView(
        suggestion_id=12,
        state=SuggestionState.PENDING,
        suggestion=make_suggestion(
            position=0,
            title="Reply to <Sam>",
            fingerprint=fingerprint_of("sam"),
            steps=(),
            deadline_text="<b>soon</b>",
            deadline_precision=DeadlinePrecision.UNRESOLVED,
        ),
    )

    view.show_digest(brief(soon))

    assert "Suggested: Reply to <Sam> (yours; due “<b>soon</b>”)" in view.toPlainText()
    assert "<b>soon" not in view.toHtml()


def test_suggestion_links_can_be_reached_from_the_keyboard(qtbot: QtBot) -> None:
    view = DigestView()
    qtbot.addWidget(view)
    view.show_digest(brief(PENDING))

    assert Qt.TextInteractionFlag.LinksAccessibleByKeyboard in view.textInteractionFlags()


async def test_accepting_then_undoing(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()
    loads = backend.loads

    window.digest.anchorClicked.emit(QUrl("mailbrief:accept/7"))
    await finish(window)

    assert backend.action_calls == [("accept_suggestion", 7)]
    assert "Accepted: Approve the budget" in window.status.text()
    assert backend.loads == loads + 1  # The brief is reloaded to show the new state.
    assert not window.undo_button.isHidden()
    assert window.undo_button.text() == "&Undo accept"

    window.undo_button.click()
    await finish(window)

    assert backend.action_calls[-1] == (
        "unaccept_action",
        "0c5e2c1d-6b8e-4f55-9d0e-2a7f3b9c1e44",
        1,
    )
    assert window.status.text() == "Undone."
    assert window.undo_button.isHidden()


async def test_dismissing_then_undoing(window: MainWindow, backend: FakeBackend) -> None:
    await window.initialize()

    window.digest.anchorClicked.emit(QUrl("mailbrief:dismiss/7"))
    await finish(window)
    assert window.undo_button.text() == "&Undo dismiss"
    window.undo_button.click()
    await finish(window)

    assert backend.action_calls == [("dismiss_suggestion", 7), ("restore_suggestion", 7)]


async def test_a_stale_suggestion_reloads_the_brief(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.action_fail = SuggestionNotFoundError("That suggestion was not found.")
    loads = backend.loads

    window.digest.anchorClicked.emit(QUrl("mailbrief:accept/7"))
    await finish(window)

    assert "no longer available" in window.status.text()
    assert backend.loads == loads + 1
    assert window.undo_button.isHidden()


async def test_undo_explains_a_change_it_cannot_reverse(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    window.digest.anchorClicked.emit(QUrl("mailbrief:accept/7"))
    await finish(window)
    backend.action_fail = ActionConflictError("changed")

    window.undo_button.click()
    await finish(window)

    assert window.status.text() == "Can't undo: it has changed since then."
    assert window.undo_button.isHidden()


async def test_a_new_brief_withdraws_the_undo_offer(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    window.digest.anchorClicked.emit(QUrl("mailbrief:dismiss/7"))
    await finish(window)
    assert not window.undo_button.isHidden()

    window.start(window._generate)
    await asyncio.sleep(0)

    assert window.undo_button.isHidden()
    window.cancel()
    await finish(window)


async def test_suggestion_links_do_nothing_while_another_operation_runs(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    window.start(window._generate)
    await asyncio.sleep(0)

    window.digest.anchorClicked.emit(QUrl("mailbrief:accept/7"))
    await asyncio.sleep(0)

    assert backend.action_calls == []
    window.cancel()
    await finish(window)


class VanishingBackend(FakeBackend):
    """Finds the saved brief once; later loads find none, as if it had been removed."""

    # FakeBackend always returns a brief; the window's backend protocol allows None.
    async def load_saved(self) -> DailyDigest | None:  # type: ignore[override]
        saved = await super().load_saved()
        return saved if self.loads == 1 else None


async def test_the_brief_stays_on_screen_when_the_reload_after_an_accept_finds_none(
    qtbot: QtBot,
) -> None:
    backend = VanishingBackend()
    backend.saved = brief(PENDING, ACCEPTED, DISMISSED)
    window = MainWindow(backend)
    qtbot.addWidget(window)
    await window.initialize()
    shown = window.digest.toPlainText()

    window.digest.anchorClicked.emit(QUrl("mailbrief:accept/7"))
    await finish(window)

    assert backend.loads == 2  # The reload after the accept found nothing...
    assert window.digest.toPlainText() == shown  # ...so the previous brief stays.
    assert "Accepted: Approve the budget" in window.status.text()
    assert window.undo_button.text() == "&Undo accept"


async def test_an_unknown_suggestion_request_starts_nothing(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    loads = backend.loads

    window._request_suggestion("archive", 7)
    await asyncio.sleep(0)

    assert window.task is None
    assert backend.action_calls == []
    assert backend.loads == loads


async def test_dismissing_a_stale_suggestion_reloads_and_offers_no_undo(
    window: MainWindow, backend: FakeBackend
) -> None:
    await window.initialize()
    backend.action_fail = SuggestionNotFoundError("That suggestion was not found.")
    loads = backend.loads

    window.digest.anchorClicked.emit(QUrl("mailbrief:dismiss/7"))
    await finish(window)

    assert backend.action_calls == [("dismiss_suggestion", 7)]
    assert "no longer available" in window.status.text()
    assert backend.loads == loads + 1
    assert window.undo_button.isHidden()


async def test_undo_with_nothing_to_undo_calls_no_backend_method(window: MainWindow) -> None:
    await window.initialize()
    spy = AsyncMock()
    window.backend = spy

    window.start(window._undo_last)
    await finish(window)

    assert spy.mock_calls == []
    assert window.undo_button.isHidden()
