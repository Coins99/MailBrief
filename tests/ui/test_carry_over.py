"""M7's desktop carry-over fixes: the brief keeps its place, capped lists say so, and busy
clicks on brief links say why nothing happened."""

import asyncio
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import ActionFilter, ActionStatus, SuggestionState, SuggestionView
from mailbrief.ui.actions_view import ActionsPanel
from mailbrief.ui.brief_detail import ACCEPT, DISMISS
from mailbrief.ui.main_window import MainWindow
from tests.factories import fingerprint_of, make_action, make_suggestion
from tests.ui.brief_view import detail
from tests.ui.test_workflow import FakeBackend

GENERATED = datetime(2026, 9, 28, 12, tzinfo=UTC)


def test_a_capped_tab_says_more_exist(qtbot: QtBot) -> None:
    panel = ActionsPanel()
    qtbot.addWidget(panel)
    done = make_action(status=ActionStatus.COMPLETED, completed_at_utc=GENERATED)
    shown = {"today": date(2026, 9, 28), "zone": ZoneInfo("UTC"), "now": GENERATED}

    panel.show_actions(ActionFilter.COMPLETED, (done,), total=250, **shown)  # type: ignore[arg-type]
    assert panel.tabs.tabText(2) == "Completed (1+)"
    panel.show_actions(ActionFilter.COMPLETED, (done,), total=1, **shown)  # type: ignore[arg-type]
    assert panel.tabs.tabText(2) == "Completed (1)"


async def test_the_window_counts_only_a_full_completed_list(qtbot: QtBot) -> None:
    backend = FakeBackend()
    completed = tuple(
        make_action(
            public_id=f"00000000-0000-4000-8000-{index:012d}",
            status=ActionStatus.COMPLETED,
            completed_at_utc=GENERATED,
        )
        for index in range(200)
    )
    backend.actions = {ActionFilter.OPEN: (make_action(),), ActionFilter.COMPLETED: completed}
    backend.action_counts = {ActionFilter.COMPLETED: 250}
    window = MainWindow(backend)
    qtbot.addWidget(window)

    await window.initialize()

    assert backend.count_calls == [ActionFilter.COMPLETED]
    assert window.actions_panel.tabs.tabText(2) == "Completed (200+)"
    assert window.actions_panel.tabs.tabText(0) == "Open (1)"


@pytest.mark.parametrize("link", [ACCEPT, DISMISS, "reply"])
async def test_a_busy_click_on_a_brief_link_says_so(qtbot: QtBot, link: str) -> None:
    backend = FakeBackend()
    backend.saved = backend.saved.model_copy(
        update={
            "items": (
                backend.saved.items[0].model_copy(
                    update={
                        "suggestions": (
                            SuggestionView(
                                suggestion_id=7,
                                state=SuggestionState.PENDING,
                                suggestion=make_suggestion(fingerprint=fingerprint_of("a")),
                            ),
                        )
                    }
                ),
            )
        }
    )
    window = MainWindow(backend)
    qtbot.addWidget(window)
    await window.initialize()
    window.start(window._generate)
    await asyncio.sleep(0)

    item = backend.saved.items[0]
    if link == "reply":
        detail(window).reply_requested.emit(backend.saved.account_id, item.message_key)
    else:
        detail(window).suggestion_requested.emit(link, 7)

    assert window.status.text() == "MailBrief is busy; try again in a moment."
    window.cancel()
    assert window.task is not None
    await window.task
    assert backend.action_calls == [] and backend.draft_calls == []
