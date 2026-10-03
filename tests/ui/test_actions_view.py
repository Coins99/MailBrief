"""The Actions pane: plain-text rows, counts, buttons, requests and safe source links."""

from datetime import UTC, date, datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import (
    Action,
    ActionFilter,
    ActionSource,
    ActionStatus,
    ActionStep,
    ThreadActivity,
)
from mailbrief.domain.analysis import ActionOwnership, DeadlinePrecision
from mailbrief.domain.drafts import DraftKind
from mailbrief.ui.actions_view import COMPLETE, DELETE, EDIT, REOPEN, SEEN, ActionsPanel
from tests.factories import make_action

UTC_ZONE = ZoneInfo("UTC")
NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
TODAY = date(2026, 10, 3)
GMAIL = "https://mail.google.com/mail/u/?authuser=me%40x.com#all/t1"


def source(link: str = GMAIL, *, available: bool = True) -> ActionSource:
    return ActionSource(
        provider_message_id="m1",
        subject="Q3 deck",
        sender_address="alex@example.com",
        web_link=link,
        received_at_utc=datetime(2026, 9, 28, 12, tzinfo=UTC),
        available=available,
        in_inbox=True if available else None,
    )


def action(number: int, **overrides: object) -> Action:
    values: dict[str, object] = {
        "public_id": f"00000000-0000-4000-8000-{number:012d}",
        "title": f"Action {number}",
        "created_at_utc": datetime(2026, 10, 3, 8, tzinfo=UTC),
        "updated_at_utc": datetime(2026, 10, 3, 8, tzinfo=UTC),
    }
    values.update(overrides)
    return make_action(**values)


@pytest.fixture
def panel(qtbot: QtBot) -> ActionsPanel:
    result = ActionsPanel()
    qtbot.addWidget(result)
    return result


def show(panel: ActionsPanel, view: ActionFilter, *actions: Action) -> None:
    panel.show_actions(view, actions, today=TODAY, zone=UTC_ZONE, now=NOW)


def row_text(panel: ActionsPanel, view: ActionFilter, row: int = 0) -> str:
    item = panel.lists[view].item(row)
    assert item is not None
    return item.text()


def test_an_exact_deadline_reads_in_the_owner_s_zone(panel: ActionsPanel) -> None:
    friday_night = action(
        4,
        deadline_text="Friday 11 PM Pacific",
        deadline_precision=DeadlinePrecision.DATETIME,
        deadline_date=date(2026, 10, 2),
        deadline_at_utc=datetime(2026, 10, 3, 6, 0, tzinfo=UTC),
        deadline_timezone="America/Los_Angeles",
    )
    toronto = ZoneInfo("America/Toronto")

    panel.show_actions(
        ActionFilter.OPEN,
        (friday_night,),
        today=date(2026, 10, 2),
        zone=toronto,
        now=datetime(2026, 10, 2, 16, tzinfo=UTC),
    )

    assert row_text(panel, ActionFilter.OPEN) == "Action 4 — due 2026-10-03 02:00"  # Saturday.


def test_rows_are_plain_text_with_dates_progress_and_state(panel: ActionsPanel) -> None:
    late = action(
        1,
        title="<b>Send</b> the deck",
        target_date=date(2026, 9, 30),
        deadline_text="by Friday",
        deadline_precision=DeadlinePrecision.DATE,
        deadline_date=date(2026, 10, 2),
        deadline_timezone="UTC",
        created_at_utc=datetime(2026, 9, 28, 12, tzinfo=UTC),
        steps=(
            ActionStep(step_id=1, position=0, text="Draft", done=True),
            ActionStep(step_id=2, position=1, text="Review", done=False),
        ),
    )
    show(panel, ActionFilter.OPEN, late)

    assert row_text(panel, ActionFilter.OPEN) == (
        "<b>Send</b> the deck — target 2026-09-30 · due 2026-10-02 · 1/2 steps · overdue · "
        "carried over"
    )
    assert panel.tabs.tabText(0) == "Open (1)"


def test_waiting_unresolved_and_unavailable_details(panel: ActionsPanel) -> None:
    waiting = action(
        2,
        ownership=ActionOwnership.WAITING_FOR,
        deadline_text="soon",
        deadline_precision=DeadlinePrecision.UNRESOLVED,
        sources=(source(available=False),),
    )
    show(panel, ActionFilter.WAITING, waiting)

    assert row_text(panel, ActionFilter.WAITING) == (
        "Action 2 — due “soon” · waiting for someone · source no longer in local mail"
    )


def test_a_completed_action_is_never_shown_overdue_or_carried_over(panel: ActionsPanel) -> None:
    done = action(
        3,
        status=ActionStatus.COMPLETED,
        completed_at_utc=NOW,
        deadline_text="by Friday",
        deadline_precision=DeadlinePrecision.DATE,
        deadline_date=date(2026, 9, 1),
        deadline_timezone="UTC",
        created_at_utc=datetime(2026, 8, 1, tzinfo=UTC),
    )
    show(panel, ActionFilter.COMPLETED, done)

    assert row_text(panel, ActionFilter.COMPLETED) == "Action 3 — due 2026-09-01"


def test_buttons_follow_the_selection_the_tab_and_busy_state(panel: ActionsPanel) -> None:
    assert not panel.edit_button.isEnabled()
    show(panel, ActionFilter.OPEN, action(1))
    show(
        panel,
        ActionFilter.COMPLETED,
        action(2, status=ActionStatus.COMPLETED, completed_at_utc=NOW),
    )

    assert panel.edit_button.isEnabled() and panel.complete_button.text() == "Com&plete"
    panel.tabs.setCurrentIndex(2)
    assert panel.complete_button.text() == "Re&open"
    panel.set_busy(True)
    assert not panel.complete_button.isEnabled()
    panel.set_busy(False)
    assert panel.complete_button.isEnabled()


def test_buttons_and_activation_request_changes_for_the_selected_action(
    panel: ActionsPanel,
) -> None:
    first, completed = action(1), action(2, status=ActionStatus.COMPLETED, completed_at_utc=NOW)
    show(panel, ActionFilter.OPEN, first)
    show(panel, ActionFilter.COMPLETED, completed)
    requests: list[tuple[str, Action]] = []
    panel.action_requested.connect(lambda kind, item: requests.append((kind, item)))

    panel.edit_button.click()
    panel.complete_button.click()
    panel.delete_button.click()
    item = panel.lists[ActionFilter.OPEN].item(0)
    panel.lists[ActionFilter.OPEN].itemActivated.emit(item)
    panel.tabs.setCurrentIndex(2)
    panel.complete_button.click()
    panel.set_busy(True)
    panel.delete_button.click()  # Disabled while busy.
    panel._request(DELETE)  # Refused even when called directly.

    assert requests == [
        (EDIT, first),
        (COMPLETE, first),
        (DELETE, first),
        (EDIT, first),
        (REOPEN, completed),
    ]


def test_a_refresh_keeps_the_selected_action_selected(panel: ActionsPanel) -> None:
    show(panel, ActionFilter.OPEN, action(1), action(2), action(3))
    panel.lists[ActionFilter.OPEN].setCurrentRow(2)

    show(panel, ActionFilter.OPEN, action(3), action(1))

    assert panel.selected() == action(3)
    show(panel, ActionFilter.OPEN)
    assert panel.selected() is None
    assert panel.tabs.tabText(0) == "Open (0)"


def test_only_a_gmail_source_can_be_opened(
    panel: ActionsPanel, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = Mock(return_value=True)
    monkeypatch.setattr(QDesktopServices, "openUrl", opened)

    show(panel, ActionFilter.OPEN, action(1, sources=(source("https://evil.example/x"),)))
    assert not panel.source_button.isEnabled()
    panel._open_source()
    opened.assert_not_called()

    show(panel, ActionFilter.OPEN, action(1, sources=(source(),)))
    assert panel.source_button.isEnabled()
    panel.source_button.click()
    opened.assert_called_once_with(QUrl(GMAIL))


def test_a_reply_draft_needs_an_email_in_local_mail(qtbot: QtBot) -> None:
    panel = ActionsPanel()
    qtbot.addWidget(panel)
    found: list[tuple[object, object]] = []
    panel.draft_requested.connect(lambda kind, action: found.append((kind, action)))
    action = make_action()
    panel.show_actions(
        ActionFilter.OPEN,
        (action,),
        today=date(2026, 9, 28),
        zone=ZoneInfo("UTC"),
        now=datetime(2026, 9, 28, tzinfo=UTC),
    )

    panel._request_draft(DraftKind.REPLY)
    panel._request_draft(DraftKind.NOTE)

    assert not panel.reply_draft.isEnabled()
    assert found == [(DraftKind.NOTE, action)]


def test_thread_activity_reads_in_the_owner_s_zone(panel: ActionsPanel) -> None:
    recent = action(
        5,
        thread=ThreadActivity(
            new_messages=2,
            latest_at_utc=datetime(2026, 9, 29, 18, 2, tzinfo=UTC),
            latest_sender="Sam <b>Lee</b>",
            owner_replied_at_utc=datetime(2026, 9, 30, 15, tzinfo=UTC),
        ),
    )
    older = action(
        6,
        thread=ThreadActivity(
            new_messages=1,
            latest_at_utc=datetime(2026, 9, 20, 14, 30, tzinfo=UTC),
            latest_sender="sam@example.com",
            owner_replied_at_utc=datetime(2026, 9, 26, 12, tzinfo=UTC),
        ),
    )
    quiet = action(7, thread=ThreadActivity())

    panel.show_actions(
        ActionFilter.OPEN,
        (recent, older, quiet),
        today=TODAY,
        zone=ZoneInfo("America/Toronto"),
        now=NOW,
    )

    assert row_text(panel, ActionFilter.OPEN, 0) == (
        "Action 5 — 2 new in thread, latest Tue 14:02 from Sam <b>Lee</b> · you replied Wed"
    )
    # More than a week back, a weekday would be ambiguous: the date is shown instead.
    assert row_text(panel, ActionFilter.OPEN, 1) == (
        "Action 6 — 1 new in thread, latest 2026-09-20 10:30 from sam@example.com · "
        "you replied 2026-09-26"
    )
    assert row_text(panel, ActionFilter.OPEN, 2) == "Action 7"


def test_mark_seen_is_offered_only_for_an_action_with_activity(panel: ActionsPanel) -> None:
    quiet = action(1, thread=ThreadActivity())
    replied = action(
        2, thread=ThreadActivity(owner_replied_at_utc=datetime(2026, 10, 2, tzinfo=UTC))
    )
    untracked = action(3)
    show(panel, ActionFilter.OPEN, quiet, replied, untracked)
    requests: list[tuple[str, Action]] = []
    panel.action_requested.connect(lambda kind, item: requests.append((kind, item)))

    assert panel.seen_button.text() == "Mar&k seen"
    assert not panel.seen_button.isEnabled()
    panel._mark_seen()  # Refused even when called directly.
    panel.lists[ActionFilter.OPEN].setCurrentRow(2)
    assert not panel.seen_button.isEnabled()
    panel.lists[ActionFilter.OPEN].setCurrentRow(1)
    assert panel.seen_button.isEnabled()
    panel.seen_button.click()
    panel.set_busy(True)
    assert not panel.seen_button.isEnabled()
    panel._mark_seen()

    assert requests == [(SEEN, replied)]
