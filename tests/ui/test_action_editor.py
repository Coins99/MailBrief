"""The action editor: it shows an action, validates the owner's edit and plan, then asks."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QDialogButtonBox, QWidget
from pytestqt.qtbot import QtBot

from mailbrief.domain.actions import Action, ActionEdit, ActionSource, ActionStep, StepEdit
from mailbrief.domain.analysis import (
    ActionEffort,
    ActionOwnership,
    DeadlinePrecision,
    TargetReason,
)
from mailbrief.ui.action_editor import ActionEditor
from tests.factories import make_action

ACTION = make_action(
    title="Send the Q3 deck",
    effort=ActionEffort.HOURS,
    deadline_text="by Friday",
    deadline_precision=DeadlinePrecision.DATE,
    deadline_date=date(2026, 10, 2),
    deadline_timezone="UTC",
    suggested_target_date=date(2026, 10, 1),
    target_reason=TargetReason.WORKING_DAY_BEFORE,
    target_date=date(2026, 10, 1),
    notes="Ask Sam.",
    steps=(
        ActionStep(step_id=11, position=0, text="Collect figures", done=True),
        ActionStep(step_id=12, position=1, text="Draft slides", done=False),
    ),
    sources=(
        ActionSource(
            provider_message_id="m1",
            subject="<i>Q3</i>",
            sender_address="alex@example.com",
            web_link="https://mail.google.com/mail/u/?authuser=me#all/t1",
            received_at_utc=datetime(2026, 9, 28, 12, tzinfo=UTC),
            available=False,
        ),
    ),
)


@pytest.fixture
def editor(qtbot: QtBot) -> Iterator[ActionEditor]:
    parent = QWidget()  # Held by this frame: qtbot keeps widgets only weakly.
    qtbot.addWidget(parent)
    result = ActionEditor(parent)
    result.edit(ACTION)
    yield result


def saved(editor: ActionEditor) -> list[tuple[Action, ActionEdit, list[StepEdit] | None]]:
    found: list[tuple[Action, ActionEdit, list[StepEdit] | None]] = []
    editor.save_requested.connect(lambda action, edit, steps: found.append((action, edit, steps)))
    return found


def save(editor: ActionEditor) -> None:
    button = editor.buttons.button(QDialogButtonBox.StandardButton.Save)
    assert button is not None
    button.click()


def test_it_shows_the_action_as_plain_text(editor: ActionEditor) -> None:
    assert editor.title.text() == "Send the Q3 deck"
    assert (editor.owner.currentText(), editor.effort.currentText()) == ("Mine", "Hours")
    assert editor.has_target.isChecked() and editor.target.date() == QDate(2026, 10, 1)
    assert editor.deadline.text() == "2026-10-02 (date only)"
    assert editor.suggested.text() == "2026-10-01, one working day before the deadline"
    assert editor.sources.text() == ("<i>Q3</i> — alex@example.com (no longer in local mail)")
    assert editor.sources.textFormat() is Qt.TextFormat.PlainText
    assert editor.notes.toPlainText() == "Ask Sam."
    assert [editor.steps.item(n).checkState() for n in range(2)] == [
        Qt.CheckState.Checked,
        Qt.CheckState.Unchecked,
    ]


def test_saving_without_plan_changes_sends_no_steps(editor: ActionEditor) -> None:
    requests = saved(editor)
    editor.title.setText("  Send the final Q3 deck  ")
    editor.owner.setCurrentIndex(1)
    editor.effort.setCurrentIndex(0)
    editor.has_target.setChecked(False)

    save(editor)

    ((action, edit, steps),) = requests
    assert action == ACTION
    assert edit == ActionEdit(
        title="Send the final Q3 deck",
        ownership=ActionOwnership.WAITING_FOR,
        effort=None,
        target_date=None,
        notes="Ask Sam.",
    )
    assert steps is None


def test_an_edited_plan_keeps_step_ids_and_order(editor: ActionEditor) -> None:
    requests = saved(editor)
    first = editor.steps.item(0)
    first.setCheckState(Qt.CheckState.Unchecked)
    editor.steps.setCurrentRow(1)
    editor.up.click()  # "Draft slides" moves above "Collect figures".
    editor.add_step.click()
    added = editor.steps.item(2)
    added.setText("Book a review")
    editor.steps.addItem("   ")  # Blank steps are dropped.

    save(editor)

    ((_, _, steps),) = requests
    assert steps == [
        StepEdit(step_id=12, text="Draft slides", done=False),
        StepEdit(step_id=11, text="Collect figures", done=False),
        StepEdit(step_id=None, text="Book a review", done=False),
    ]


def test_removing_a_step_is_a_plan_change(editor: ActionEditor) -> None:
    requests = saved(editor)
    editor.steps.setCurrentRow(0)
    editor.remove_step.click()

    save(editor)

    assert requests[0][2] == [StepEdit(step_id=12, text="Draft slides", done=False)]


@pytest.mark.parametrize("problem", ["title", "notes"])
def test_invalid_input_is_refused_with_a_reason(editor: ActionEditor, problem: str) -> None:
    requests = saved(editor)
    if problem == "title":
        editor.title.setText("   ")
    else:
        editor.notes.setPlainText("x" * 10_001)

    save(editor)

    assert requests == []
    assert editor.status.text() in {"Enter a title.", "Notes can be at most 10,000 characters."}


def test_a_plan_cannot_grow_past_thirty_steps(editor: ActionEditor) -> None:
    for _ in range(40):
        editor._add_step()

    assert editor.steps.count() == 30
    assert not editor.add_step.isEnabled()


def test_busy_blocks_saving(editor: ActionEditor) -> None:
    requests = saved(editor)
    editor.set_busy(True)
    editor._save()
    assert requests == []
    editor.set_busy(False)
    save(editor)
    assert len(requests) == 1


def test_each_edit_starts_from_the_new_action(editor: ActionEditor) -> None:
    editor.title.setText("Unsaved change")
    editor.edit(make_action(title="Another action"))

    assert editor.title.text() == "Another action"
    assert editor.steps.count() == 0
    assert editor.sources.text() == "no source"
    assert editor.suggested.text() == "no target was suggested"
    assert editor.deadline.text() == "none stated"


@pytest.mark.parametrize(
    ("deadline", "shown"),
    [
        (
            {
                "deadline_text": "Friday 5 PM",
                "deadline_precision": DeadlinePrecision.DATETIME,
                "deadline_date": date(2026, 10, 2),
                "deadline_at_utc": datetime(2026, 10, 2, 21, 0, tzinfo=UTC),
                "deadline_timezone": "America/Toronto",
            },
            "2026-10-02T17:00-04:00 (as the email states)",
        ),
        (
            {"deadline_text": "soon", "deadline_precision": DeadlinePrecision.UNRESOLVED},
            "“soon” (no specific day)",
        ),
    ],
    ids=["exact-time-in-its-zone", "unresolved-phrase"],
)
def test_a_deadline_is_shown_as_the_email_states_it(
    editor: ActionEditor, deadline: dict[str, object], shown: str
) -> None:
    editor.edit(make_action(**deadline))

    assert editor.deadline.text() == shown


def test_remove_and_moves_past_either_end_change_nothing(editor: ActionEditor) -> None:
    requests = saved(editor)
    before = editor._current_steps()

    editor.steps.setCurrentRow(-1)
    assert not editor.remove_step.isEnabled()
    editor._remove_step()  # Refused even when called directly.
    editor.steps.setCurrentRow(0)
    assert not editor.up.isEnabled()
    editor._move(-1)
    editor.steps.setCurrentRow(1)
    assert not editor.down.isEnabled()
    editor._move(1)

    assert editor._current_steps() == before
    save(editor)
    assert requests[0][2] is None  # The plan is unchanged, so no steps are sent.


def test_a_step_over_500_characters_is_refused_with_the_reason(editor: ActionEditor) -> None:
    requests = saved(editor)
    step = editor.steps.item(1)
    step.setText("x" * 501)

    save(editor)

    assert requests == []
    assert editor.status.text() == "A step can be at most 500 characters."
    step.setText("x" * 500)
    save(editor)
    assert len(requests) == 1


def test_a_title_with_markup_appears_literally(editor: ActionEditor) -> None:
    editor.edit(make_action(title="<b>x</b> the deck"))

    assert editor.title.text() == "<b>x</b> the deck"


CHICAGO_FIVE = make_action(
    deadline_text="Friday 4 PM Central",
    deadline_precision=DeadlinePrecision.DATETIME,
    deadline_date=date(2026, 10, 2),
    deadline_at_utc=datetime(2026, 10, 2, 21, 0, tzinfo=UTC),
    deadline_timezone="America/Chicago",
)


def test_an_exact_deadline_reads_in_the_owner_s_zone_like_the_brief(editor: ActionEditor) -> None:
    editor.edit(CHICAGO_FIVE, ZoneInfo("America/Toronto"))

    assert editor.deadline.text() == (
        "2026-10-02T17:00-04:00 (16:00 America/Chicago as the email states)"
    )

    editor.edit(CHICAGO_FIVE, ZoneInfo("America/Chicago"))
    assert editor.deadline.text() == "2026-10-02T16:00-05:00 (as the email states)"


@pytest.mark.parametrize(
    "zone", [None, "UTC", "Pacific/Kiritimati", "Pacific/Pago_Pago", "America/Toronto"]
)
def test_a_date_deadline_reads_the_same_in_any_owner_zone(
    editor: ActionEditor, zone: str | None
) -> None:
    dated = make_action(
        deadline_text="by Friday",
        deadline_precision=DeadlinePrecision.DATE,
        deadline_date=date(2026, 10, 2),
        deadline_timezone="America/Toronto",
    )

    editor.edit(dated, None if zone is None else ZoneInfo(zone))

    assert editor.deadline.text() == "2026-10-02 (date only)"


def test_an_owner_in_utc_sees_utc_and_the_email_s_own_time(editor: ActionEditor) -> None:
    toronto_five = make_action(
        deadline_text="Friday 5 PM",
        deadline_precision=DeadlinePrecision.DATETIME,
        deadline_date=date(2026, 10, 2),
        deadline_at_utc=datetime(2026, 10, 2, 21, 0, tzinfo=UTC),
        deadline_timezone="America/Toronto",
    )

    editor.edit(toronto_five, ZoneInfo("UTC"))

    assert editor.deadline.text() == (
        "2026-10-02T21:00+00:00 (17:00 America/Toronto as the email states)"
    )
