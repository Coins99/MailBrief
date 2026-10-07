"""The refresh settings in the Preferences tab, and the automatic-analysis dialog (ADR 0017)."""

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from mailbrief.domain.preferences import REFRESH_INTERVALS, OwnerPreferences, PreferencesEdit
from mailbrief.services.consent import NO_CONSENT
from mailbrief.ui.auto_send_view import OFF_TEXT, AutoSendDialog
from mailbrief.ui.preferences_view import REFRESH_CHOICES, PreferencesPanel
from tests.factories import make_auto_send

TORONTO = ZoneInfo("America/Toronto")


@pytest.fixture
def panel(qtbot: QtBot) -> PreferencesPanel:
    result = PreferencesPanel()
    qtbot.addWidget(result)
    result.set_zones("Asia/Tokyo", ("America/Toronto", "UTC"))
    return result


def saved(panel: PreferencesPanel) -> PreferencesEdit:
    edits: list[PreferencesEdit] = []
    panel.save_requested.connect(lambda edit, _revision: edits.append(edit))
    panel.save_button.click()
    (edit,) = edits
    return edit


# The refresh settings


def test_the_choices_are_off_and_the_three_intervals_the_app_accepts() -> None:
    assert [minutes for _name, minutes in REFRESH_CHOICES] == [None, *REFRESH_INTERVALS]
    assert [name for name, _minutes in REFRESH_CHOICES] == [
        "Off",
        "Every hour",
        "Every 2 hours",
        "Every 4 hours",
    ]


def test_nothing_refreshes_by_default(panel: PreferencesPanel) -> None:
    panel.set_preferences(OwnerPreferences.defaults())

    assert not panel.refresh_on_launch.isChecked()
    assert panel.refresh_interval.currentText() == "Off"
    edit = saved(panel)
    assert (edit.refresh_on_launch, edit.refresh_interval_minutes) == (False, None)


@pytest.mark.parametrize(
    ("launch", "minutes", "label"),
    [
        (True, None, "Off"),
        (False, 60, "Every hour"),
        (True, 120, "Every 2 hours"),
        (False, 240, "Every 4 hours"),
    ],
)
def test_the_refresh_settings_round_trip_through_the_form(
    panel: PreferencesPanel, launch: bool, minutes: int | None, label: str
) -> None:
    panel.set_preferences(
        OwnerPreferences(revision=2, refresh_on_launch=launch, refresh_interval_minutes=minutes)
    )

    assert panel.refresh_on_launch.isChecked() is launch
    assert panel.refresh_interval.currentText() == label
    edit = saved(panel)
    assert (edit.refresh_on_launch, edit.refresh_interval_minutes) == (launch, minutes)


def test_choosing_in_the_form_is_what_is_saved(panel: PreferencesPanel) -> None:
    panel.set_preferences(OwnerPreferences.defaults())

    panel.refresh_on_launch.setChecked(True)
    panel.refresh_interval.setCurrentIndex(2)

    edit = saved(panel)
    assert (edit.refresh_on_launch, edit.refresh_interval_minutes) == (True, 120)


def test_the_new_controls_are_named_and_keep_every_mnemonic_distinct(
    panel: PreferencesPanel,
) -> None:
    for control in (
        panel.refresh_on_launch,
        panel.refresh_interval,
        panel.auto_line,
        panel.auto_button,
    ):
        assert control.accessibleName() or isinstance(control, QCheckBox)

    texts = [
        *(label.text() for label in panel.findChildren(QLabel)),
        *(box.text() for box in panel.findChildren(QCheckBox)),
        *(button.text() for button in panel.findChildren(QPushButton)),
    ]
    marked = [re.search(r"(?<!&)&(?!&)(.)", text) for text in texts]
    letters = [found.group(1).lower() for found in marked if found is not None]
    assert {"r", "w", "h"} <= set(letters)  # Refresh, while, cHange.
    assert len(letters) == len(set(letters))  # No two share a key.


# The automatic-analysis line


def line(panel: PreferencesPanel) -> tuple[str, bool]:
    return panel.auto_line.text(), panel.auto_button.isEnabled()


def test_without_a_consent_there_is_nothing_to_change_yet(panel: PreferencesPanel) -> None:
    panel.set_auto_send(None, TORONTO)

    assert line(panel) == ("Analyze once with Sync and review to give consent first.", False)
    assert line(panel)[0] == NO_CONSENT


def test_off_says_every_run_asks_first(panel: PreferencesPanel) -> None:
    panel.set_auto_send(make_auto_send(limit=0), TORONTO)

    assert line(panel) == ("Off — every run asks you first", True)


@pytest.mark.parametrize(
    ("limit", "granted", "text"),
    [
        (1, datetime(2026, 9, 30, 14, tzinfo=UTC), "Up to 1 message per run, since 2026-09-30"),
        (3, datetime(2026, 9, 30, 14, tzinfo=UTC), "Up to 3 messages per run, since 2026-09-30"),
        # 02:00 UTC is still the evening before in Toronto: the date is the owner's.
        (10, datetime(2026, 9, 30, 2, tzinfo=UTC), "Up to 10 messages per run, since 2026-09-29"),
    ],
)
def test_a_permission_says_how_many_and_since_when_in_the_owners_zone(
    panel: PreferencesPanel, limit: int, granted: datetime, text: str
) -> None:
    panel.set_auto_send(make_auto_send(limit=limit, granted_at_utc=granted), TORONTO)

    assert line(panel) == (text, True)


def test_a_setting_that_could_not_be_read_can_not_be_changed(panel: PreferencesPanel) -> None:
    panel.set_auto_send(None, TORONTO, unreadable=True)

    text, changeable = line(panel)
    assert "couldn't be read" in text and not changeable


def test_change_asks_the_window_and_changes_nothing_itself(panel: PreferencesPanel) -> None:
    asked: list[bool] = []
    panel.auto_send_requested.connect(lambda: asked.append(True))
    panel.set_auto_send(make_auto_send(limit=2), TORONTO)

    panel.auto_button.click()
    panel.set_auto_send(None, TORONTO)
    panel.auto_button.click()  # Disabled without a consent.

    assert asked == [True]
    # The permission is never part of the form: saving sends the same edit either way.
    assert set(PreferencesEdit.model_fields) >= {"refresh_on_launch", "refresh_interval_minutes"}
    assert not [name for name in PreferencesEdit.model_fields if "auto_send" in name]


def test_plain_text_only(panel: PreferencesPanel) -> None:
    panel.set_auto_send(make_auto_send(limit=2, account_email="<b>me</b>@example.com"), TORONTO)

    assert panel.auto_line.textFormat() == Qt.TextFormat.PlainText
    assert "<b>" not in panel.auto_line.text()


# The dialog


@pytest.fixture
def dialog(qtbot: QtBot) -> AutoSendDialog:
    parent = QWidget()
    qtbot.addWidget(parent)
    result = AutoSendDialog(parent)
    result._keep = parent  # type: ignore[attr-defined]  # qtbot holds widgets weakly.
    return result


def test_off_shows_a_zero_as_off_and_says_nothing_is_sent(dialog: AutoSendDialog) -> None:
    dialog.configure(make_auto_send(limit=0))

    assert dialog.limit.value() == 0 and dialog.limit.text() == "Off"
    assert dialog.disclosure.text() == OFF_TEXT


def test_a_number_shows_the_disclosure_and_the_permission_sentence(
    dialog: AutoSendDialog,
) -> None:
    dialog.configure(make_auto_send(limit=0))

    dialog.limit.setValue(3)

    lines = dialog.disclosure.text().split("\n\n")
    assert lines[0] == "MailBrief will send 3 messages to Groq (test-model) for analysis."
    assert "Enable Zero Data Retention." in lines
    assert lines[-1] == (
        "Automatic runs may send up to 3 messages from owner@example.com to Groq without "
        "asking. Revoke consent in Settings to stop."
    )
    dialog.limit.setValue(1)
    assert "MailBrief will send 1 message to Groq" in dialog.disclosure.text()
    assert "up to 1 message from owner@example.com" in dialog.disclosure.text()
    dialog.limit.setValue(0)
    assert dialog.disclosure.text() == OFF_TEXT


def test_it_opens_on_the_current_permission(dialog: AutoSendDialog) -> None:
    dialog.configure(make_auto_send(limit=4))

    assert dialog.limit.value() == 4
    assert "send 4 messages" in dialog.disclosure.text()


def test_the_limit_stays_within_zero_to_ten(dialog: AutoSendDialog) -> None:
    dialog.configure(make_auto_send())

    dialog.limit.setValue(99)
    assert dialog.limit.value() == 10
    dialog.limit.setValue(-5)
    assert dialog.limit.value() == 0


def test_save_emits_the_choice_and_closes_and_cancel_emits_nothing(
    dialog: AutoSendDialog,
) -> None:
    chosen: list[int] = []
    dialog.save_requested.connect(chosen.append)
    dialog.configure(make_auto_send(limit=2))
    dialog.show()

    dialog.limit.setValue(5)
    dialog.save_button.click()

    assert chosen == [5] and not dialog.isVisible()
    dialog.configure(make_auto_send(limit=2))
    dialog.show()
    dialog.limit.setValue(9)
    dialog.cancel_button.click()
    assert chosen == [5] and not dialog.isVisible()


def test_turning_it_off_is_saved_as_zero(dialog: AutoSendDialog) -> None:
    chosen: list[int] = []
    dialog.save_requested.connect(chosen.append)
    dialog.configure(make_auto_send(limit=3))

    dialog.limit.setValue(0)
    dialog.save_button.click()

    assert chosen == [0]


def test_everything_shown_is_plain_text_with_accessible_names(dialog: AutoSendDialog) -> None:
    dialog.configure(make_auto_send(limit=2, account_email="<i>me</i>@example.com"))

    assert dialog.disclosure.textFormat() == Qt.TextFormat.PlainText
    assert "<i>me</i>@example.com" in dialog.disclosure.text()  # Shown as typed, never as markup.
    assert dialog.limit.accessibleName() and dialog.disclosure.accessibleName()
    assert {widget.text() for widget in (dialog.save_button, dialog.cancel_button)} == {
        "&Save",
        "&Cancel",
    }
