"""Settings interaction, secret-field lifetime and shutdown during durable writes."""

import asyncio

from pydantic import SecretStr
from PySide6.QtWidgets import QLineEdit
from pytestqt.qtbot import QtBot

from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.preferences import DesktopPreferences
from tests.ui.test_workflow import FakeBackend, finish


async def test_settings_save_key_clear_and_revoke(qtbot: QtBot) -> None:
    backend = FakeBackend()
    window = MainWindow(backend)
    qtbot.addWidget(window)
    window.settings_button.click()
    await finish(window)
    editor = window.settings_dialog
    assert editor.isVisible()
    editor.model.setText("test-model")
    editor.save_button.click()
    assert not window.cancel_button.isEnabled()
    await finish(window)
    assert backend.preferences.groq_model == "test-model"
    assert "Settings saved" in editor.status.text()
    assert editor.key.echoMode() == QLineEdit.EchoMode.Password
    editor.key.setText("synthetic-key-not-a-real-credential")
    editor.key_button.click()
    assert editor.key.text() == ""
    await finish(window)
    saved_key = backend.key_value
    assert saved_key == SecretStr("synthetic-key-not-a-real-credential")
    assert "synthetic-key" not in editor.status.text()
    editor.remove_key_button.click()
    await finish(window)
    assert backend.key_value is None
    editor.revoke_button.click()
    await finish(window)
    assert backend.revoked
    editor.key.setText("unsaved-secret")
    editor.reject()
    assert editor.key.text() == ""


async def test_invalid_key_never_reaches_backend(qtbot: QtBot) -> None:
    backend = FakeBackend()
    window = MainWindow(backend)
    qtbot.addWidget(window)
    window.settings_button.click()
    await finish(window)
    editor = window.settings_dialog
    editor.key.setText("tiny")
    editor.key_button.click()
    assert backend.key_value is None
    assert "invalid" in editor.status.text()
    assert editor.key.text() == ""


async def test_close_waits_for_settings_write(qtbot: QtBot) -> None:
    started = asyncio.Event()
    finish_write = asyncio.Event()

    class SlowBackend(FakeBackend):
        async def save_preferences(self, preferences: DesktopPreferences) -> None:
            started.set()
            await finish_write.wait()
            await super().save_preferences(preferences)

    backend = SlowBackend()
    window = MainWindow(backend)
    qtbot.addWidget(window)
    window._request_save_preferences(DesktopPreferences(groq_model="new-model"))
    await started.wait()
    window.close()
    shutdown = asyncio.create_task(window.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()
    assert not backend.closed
    finish_write.set()
    await shutdown
    assert backend.preferences.groq_model == "new-model"
    assert backend.closed
