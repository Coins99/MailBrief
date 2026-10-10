"""The draft editor alone: fields per kind, live checks, autosave timing, versions, closing."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from pydantic import HttpUrl
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QFileDialog, QWidget
from pytestqt.qtbot import QtBot

from mailbrief.domain.drafts import (
    Draft,
    DraftEdit,
    DraftKind,
    DraftSource,
    DraftVersion,
    DraftVersionInfo,
    DraftVersionOrigin,
)
from mailbrief.ui.draft_editor import CONFLICT, NOT_SAVED, DraftEditor, gmail_link

AT = datetime(2026, 9, 28, 14, 5, tzinfo=UTC)
GMAIL = "https://mail.google.com/mail/u/?authuser=me#all/m1"
SOURCE = DraftSource(
    provider_message_id="m1",
    subject="Budget",
    sender_address="alex@example.com",
    web_link=HttpUrl(GMAIL),
    received_at_utc=AT,
    available=True,
)


def make_draft(**overrides: Any) -> Draft:
    values: dict[str, Any] = {
        "public_id": "00000000-0000-4000-8000-000000000001",
        "kind": DraftKind.REPLY,
        "title": "Re: Budget",
        "to_text": "alex@example.com",
        "sources": (SOURCE,),
        "created_at_utc": AT,
        "updated_at_utc": AT,
        "revision": 1,
    }
    values.update(overrides)
    return Draft.model_validate(values)


@pytest.fixture
def editor(qtbot: QtBot) -> Iterator[DraftEditor]:
    parent = QWidget()  # Held by this frame: qtbot keeps widgets only weakly.
    qtbot.addWidget(parent)
    result = DraftEditor(parent, debounce_ms=60_000, max_wait_ms=60_000, retry_ms=60_000)
    result.load(make_draft(), ZoneInfo("America/Toronto"))
    yield result


def recorded(signal: Any) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    signal.connect(lambda *args: calls.append(args))
    return calls


def test_a_reply_shows_recipients_subject_and_its_source(editor: DraftEditor) -> None:
    assert editor.to_edit.text() == "alex@example.com"
    assert editor.title_edit.text() == "Re: Budget"
    assert editor.title_label.text() == "&Subject"
    assert not editor.to_edit.isHidden() and not editor.copy_subject_button.isHidden()
    assert editor.context.text() == "From email: Budget — alex@example.com"
    assert not editor.gmail_button.isHidden()
    assert editor.status.text() == "Saved 10:05"  # In the owner's zone.
    assert editor.windowTitle() == "Reply draft"


def test_a_note_has_no_recipients_and_names_its_action(editor: DraftEditor) -> None:
    unavailable = SOURCE.model_copy(update={"available": False})
    editor.load(
        make_draft(
            kind=DraftKind.NOTE,
            title="Plan",
            to_text="",
            action_title="Send the deck",
            sources=(unavailable,),
        ),
        ZoneInfo("UTC"),
    )

    assert editor.title_label.text() == "&Title"
    assert editor.copy_subject_button.isHidden()
    assert editor.context.text() == (
        "From email: Budget — alex@example.com · source no longer in local mail · "
        "For action: “Send the deck”"
    )
    assert editor.current_edit() == DraftEdit(title="Plan")


def test_an_unlinked_draft_says_so_and_only_gmail_links_open(editor: DraftEditor) -> None:
    editor.load(make_draft(kind=DraftKind.MESSAGE, title="", to_text="", sources=()), AT.tzinfo)  # type: ignore[arg-type]
    assert editor.context.text() == "Not linked to an email or action."
    assert editor.gmail_button.isHidden()
    other = SOURCE.model_copy(update={"web_link": HttpUrl("https://evil.example/m1")})
    assert gmail_link(other) is None
    assert gmail_link(SOURCE) == GMAIL


def test_open_in_gmail(editor: DraftEditor, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = Mock()
    monkeypatch.setattr(QDesktopServices, "openUrl", opened)
    editor.gmail_button.click()
    opened.assert_called_once_with(QUrl(GMAIL))


def test_live_counter_placeholders_and_recipient_warnings(editor: DraftEditor) -> None:
    editor.to_edit.setText("alex@example.com, sam")
    editor.cc_edit.setText("kim@")
    editor.body.setPlainText("Hi [[name]], see [[date]] and [[name]].")

    assert editor.recipient_warning.text() == "Check these recipients: sam; kim@"
    assert editor.placeholder_line.text() == "Placeholders to fill: [[name]], [[date]]"
    assert editor.counter.text() == "39 / 20,000 characters"
    editor.body.setPlainText("Done.")
    assert editor.placeholder_line.text() == ""


def test_typing_autosaves_once_after_the_pause(editor: DraftEditor, qtbot: QtBot) -> None:
    editor.set_timing(debounce_ms=100, max_wait_ms=60_000, retry_ms=60_000)
    saves = recorded(editor.autosave_requested)

    editor.body.setPlainText("a")
    assert editor.status.text() == "Saving…"
    editor.body.setPlainText("ab")  # Before any event runs, so the pause restarts.
    qtbot.waitUntil(lambda: len(saves) == 1, timeout=5_000)
    qtbot.wait(300)

    assert [edit.body for (edit,) in saves] == ["ab"]
    assert not editor.dirty


def test_continuous_typing_still_saves_at_the_maximum_interval(
    editor: DraftEditor, qtbot: QtBot
) -> None:
    editor.set_timing(debounce_ms=60_000, max_wait_ms=250, retry_ms=60_000)
    saves = recorded(editor.autosave_requested)

    for text in ("a", "ab", "abc"):
        editor.body.setPlainText(text)
        qtbot.wait(100)
    # The pause never ends, yet the text is saved while typing continues.
    qtbot.waitUntil(lambda: len(saves) >= 1, timeout=5_000)

    assert saves[0][0].body in {"a", "ab", "abc"}


def test_a_saved_answer_shows_the_time_only_without_newer_changes(editor: DraftEditor) -> None:
    editor.body.setPlainText("Hello")
    editor._autosave_now()
    edit = editor.current_edit()
    assert edit is not None
    saved = make_draft(
        body="Hello", revision=2, updated_at_utc=datetime(2026, 9, 28, 15, 7, tzinfo=UTC)
    )

    editor.body.setPlainText("Hello again")  # Typed while the save ran.
    editor.saved(saved, edit)
    assert editor.status.text() == "Saving…"
    assert editor.draft == saved

    editor._autosave_now()
    newer = editor.current_edit()
    assert newer is not None
    editor.saved(saved.model_copy(update={"revision": 3}), newer)
    assert editor.status.text() == "Saved 11:07"


def test_a_failed_save_keeps_the_text_and_retries(editor: DraftEditor, qtbot: QtBot) -> None:
    editor.set_timing(debounce_ms=60_000, max_wait_ms=60_000, retry_ms=50)
    saves = recorded(editor.autosave_requested)
    editor.body.setPlainText("Keep me")
    editor._autosave_now()

    editor.save_failed()

    assert editor.status.text() == NOT_SAVED
    assert editor.body.toPlainText() == "Keep me"
    qtbot.waitUntil(lambda: len(saves) == 2, timeout=2_000)
    assert saves[1][0].body == "Keep me"


def test_a_body_over_the_limit_pauses_autosave(editor: DraftEditor, qtbot: QtBot) -> None:
    editor.set_timing(debounce_ms=30, max_wait_ms=30, retry_ms=30)
    saves = recorded(editor.autosave_requested)

    editor.body.setPlainText("x" * 20_001)
    qtbot.wait(150)

    assert saves == []
    assert "Autosave is paused" in editor.status.text()
    assert editor.current_edit() is None
    editor._checkpoint()
    assert "Autosave is paused" in editor.status.text()
    editor.body.setPlainText("x" * 20_000)
    qtbot.waitUntil(lambda: len(saves) == 1, timeout=2_000)


def test_a_conflict_stops_autosave_and_offers_a_new_draft(
    editor: DraftEditor, qtbot: QtBot
) -> None:
    editor.set_timing(debounce_ms=20, max_wait_ms=20, retry_ms=20)
    saves = recorded(editor.autosave_requested)
    copies = recorded(editor.save_as_new_requested)

    editor.show_conflict()
    editor.body.setPlainText("Still mine")
    qtbot.wait(100)

    assert saves == []
    assert editor.status.text() == CONFLICT
    assert not editor.save_as_new_button.isHidden()
    assert not editor.checkpoint_button.isEnabled()
    editor.save_as_new_button.click()
    assert copies[0][0].body == "Still mine"

    copy = make_draft(public_id="00000000-0000-4000-8000-000000000002", body="Still mine")
    editor.continue_as(copy)
    assert editor.save_as_new_button.isHidden()
    assert editor.status.text() == "Saved as a new draft at 10:05."
    assert editor.draft == copy and not editor.in_conflict


def test_copy_text_and_subject_warn_about_placeholders(editor: DraftEditor) -> None:
    editor.body.setPlainText("Hi [[name]]")
    editor.copy_text_button.click()
    assert QGuiApplication.clipboard().text() == "Hi [[name]]"
    assert editor.status.text() == "Copied the text. 1 placeholder still needs filling."

    editor.body.setPlainText("Hi Alex")
    editor.copy_subject_button.click()
    assert QGuiApplication.clipboard().text() == "Re: Budget"
    assert editor.status.text() == "Copied the subject."


def test_export_builds_the_document_from_the_current_text(editor: DraftEditor) -> None:
    exports = recorded(editor.export_requested)
    editor.body.setPlainText("Thanks!")

    editor.export_to("/tmp/out.md")
    editor.export_to("/tmp/out.TXT")
    editor.export_to("")

    assert exports == [
        ("/tmp/out.md", "**To:** alex@example.com  \n**Subject:** Re: Budget\n\nThanks!\n"),
        ("/tmp/out.TXT", "To: alex@example.com\nSubject: Re: Budget\n\nThanks!\n"),
    ]
    assert editor.status.text() == "Exporting…"


def test_export_asks_where_to_save(editor: DraftEditor, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(QFileDialog, "open", lambda self: None)
    exports = recorded(editor.export_requested)

    editor.export_button.click()
    picker = editor._picker
    assert picker is not None
    assert picker.acceptMode() is QFileDialog.AcceptMode.AcceptSave
    picker.fileSelected.emit("/tmp/Re Budget.md")

    assert exports[0][0] == "/tmp/Re Budget.md"


def test_save_version_saves_pending_text_first(editor: DraftEditor) -> None:
    order: list[str] = []
    editor.autosave_requested.connect(lambda _edit: order.append("autosave"))
    editor.checkpoint_requested.connect(lambda: order.append("checkpoint"))
    editor.body.setPlainText("New")

    editor.checkpoint_button.click()

    assert order == ["autosave", "checkpoint"]
    info = DraftVersionInfo(
        number=3, origin=DraftVersionOrigin.EDITED, created_at_utc=AT, preview="New", length=3
    )
    editor.checkpointed(info)
    assert editor.status.text() == "Saved version 3."
    editor.checkpointed(None)
    assert editor.status.text() == "No changes since the last saved version."


VERSIONS = (
    DraftVersionInfo(
        number=2, origin=DraftVersionOrigin.EDITED, created_at_utc=AT, preview="Later", length=5
    ),
    DraftVersionInfo(
        number=1, origin=DraftVersionOrigin.CREATED, created_at_utc=AT, preview="", length=0
    ),
)


def test_versions_preview_and_restore_after_confirmation(editor: DraftEditor) -> None:
    listed = recorded(editor.versions_requested)
    previews = recorded(editor.version_requested)
    restores = recorded(editor.restore_requested)

    editor.versions_button.click()
    assert listed == [()]
    editor.show_versions(VERSIONS)
    assert editor.versions_list.item(0).text() == (
        "Version 2 · saved Mon Sep 28, 10:05 · 5 characters · Later"
    )
    assert editor.versions_list.item(1).text().endswith("· (empty)")
    assert previews == [(2,)]
    editor.versions_list.setCurrentRow(1)
    assert previews[-1] == (1,)
    editor.show_version(DraftVersion(number=2, origin="edited", body="Stale", created_at_utc=AT))
    assert editor.version_preview.toPlainText() == ""  # Not the selected one.
    editor.show_version(DraftVersion(number=1, origin="created", body="", created_at_utc=AT))

    editor.restore_button.click()
    assert not editor.confirm_panel.isHidden()
    assert editor.confirm_button.text() == "Restore version 1"
    assert restores == []
    editor.confirm_button.click()
    assert restores == [(1,)]
    assert not editor.body.isEnabled() or not editor._fields.isEnabled()

    restored = make_draft(title="Re: Budget", body="", revision=4)
    editor.restored(restored, 1)
    assert editor._fields.isEnabled()
    assert editor.status.text() == ("Restored version 1. Your earlier text was saved as a version.")
    assert listed == [(), ()]  # The list is refreshed.


def test_keep_my_text_cancels_a_restore_and_a_failure_unlocks(editor: DraftEditor) -> None:
    restores = recorded(editor.restore_requested)
    editor.show_versions(VERSIONS)
    editor.restore_button.click()
    editor.keep_button.click()
    assert editor.confirm_panel.isHidden() and restores == []

    editor.restore_button.click()
    editor.confirm_button.click()
    editor.restore_failed("Couldn't restore that version; try again.")
    assert editor._fields.isEnabled()
    assert editor.status.text() == "Couldn't restore that version; try again."
    editor.versions_button.click()
    assert editor.versions_panel.isHidden()


def test_close_asks_the_window_and_waits(editor: DraftEditor, qtbot: QtBot) -> None:
    closes = recorded(editor.close_requested)
    editor.show()
    editor.body.setPlainText("Final words")

    editor.close_button.click()

    assert closes[0][0] == DraftEdit(
        title="Re: Budget", to_text="alex@example.com", body="Final words"
    )
    assert editor.isVisible() and not editor._fields.isEnabled()
    editor.reject()  # Esc while waiting does nothing more.
    assert len(closes) == 1
    editor.finish_closed()
    assert not editor.isVisible()


def test_a_clean_draft_closes_without_resending_its_text(editor: DraftEditor) -> None:
    closes = recorded(editor.close_requested)
    editor.show()
    editor.reject()
    assert closes == [(None,)]


def test_a_failed_close_stays_open_then_close_again_discards(editor: DraftEditor) -> None:
    closes = recorded(editor.close_requested)
    editor.show()
    editor.body.setPlainText("Unsaved")
    editor.reject()

    editor.cancel_close("Couldn't save. Your text is still here; try again.")

    assert editor.isVisible() and editor._fields.isEnabled()
    assert editor.body.toPlainText() == "Unsaved"
    assert editor.status.text().endswith(
        "Press Close again to close without saving your latest changes."
    )
    assert editor.final_edit() is not None
    editor.reject()
    assert not editor.isVisible()
    assert len(closes) == 1


def test_closing_in_conflict_warns_first(editor: DraftEditor) -> None:
    closes = recorded(editor.close_requested)
    editor.show()
    editor.show_conflict()

    editor.reject()
    assert editor.isVisible()
    assert "Save it as a new draft, or press Close again" in editor.status.text()
    editor.reject()
    assert not editor.isVisible() and closes == []


def test_closing_with_a_body_over_the_limit_warns_first(editor: DraftEditor) -> None:
    editor.show()
    editor.body.setPlainText("x" * 20_001)
    editor.reject()
    assert editor.isVisible() and "Autosave is paused" in editor.status.text()
    assert editor.final_edit() is None
    editor.reject()
    assert not editor.isVisible()


def test_a_conflict_while_closing_keeps_the_editor_open(editor: DraftEditor) -> None:
    editor.show()
    editor.body.setPlainText("Mine")
    editor.reject()
    editor.show_conflict()
    editor.cancel_close()
    assert editor.isVisible()
    assert (
        editor.status.text()
        == f"{CONFLICT} Press Close again to close without saving your latest changes."
    )


def test_force_close_closes_even_while_waiting(editor: DraftEditor) -> None:
    editor.show()
    editor.body.setPlainText("Mine")
    assert editor.final_edit() == editor.current_edit()
    editor.reject()
    editor.force_close()
    assert not editor.isVisible()


def test_guards_without_a_draft(qtbot: QtBot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    editor = DraftEditor(parent)
    exports = recorded(editor.export_requested)
    closes = recorded(editor.close_requested)

    editor.export_to("/tmp/x.md")
    editor._choose_export()
    editor._open_gmail()
    editor.body.setPlainText("typed before any draft")

    assert exports == [] and editor._picker is None
    assert editor.export_document("/tmp/x.md") == ""
    assert editor.final_edit() is None
    editor.show()
    editor.reject()
    assert closes == [] and not editor.isVisible()


def test_over_the_limit_every_write_waits(editor: DraftEditor) -> None:
    saves = recorded(editor.autosave_requested)
    restores = recorded(editor.restore_requested)
    copies = recorded(editor.save_as_new_requested)
    editor.show_versions(VERSIONS)
    editor.body.setPlainText("x" * 20_001)

    editor._autosave_now()
    editor.restore_button.click()
    editor.confirm_button.click()
    assert "Autosave is paused" in editor.status.text()
    editor.show_conflict()
    editor.save_as_new_button.click()

    assert saves == restores == copies == []
    assert editor.status.text() == "Shorten the body to 20,000 characters first."


def test_continuing_with_newer_text_schedules_a_save(editor: DraftEditor, qtbot: QtBot) -> None:
    editor.set_timing(debounce_ms=20, max_wait_ms=60_000, retry_ms=60_000)
    saves = recorded(editor.autosave_requested)
    editor.show_conflict()
    editor.body.setPlainText("Newer than the copy")

    editor.continue_as(make_draft(public_id="00000000-0000-4000-8000-000000000002"))

    qtbot.waitUntil(lambda: len(saves) == 1, timeout=2_000)
    assert saves[0][0].body == "Newer than the copy"


def test_versions_refresh_after_a_checkpoint_and_restore_needs_a_choice(
    editor: DraftEditor,
) -> None:
    listed = recorded(editor.versions_requested)
    restores = recorded(editor.restore_requested)
    editor.show_versions(())
    assert not editor.restore_button.isEnabled()

    editor._ask_restore()
    editor._confirm_restore()
    editor.checkpointed(None)

    assert editor.confirm_panel.isHidden() and restores == []
    assert listed == [()]
