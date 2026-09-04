"""Smoke tests for the Qt application shell."""

import os
import sys

import pytest
from PySide6.QtWidgets import QApplication, QLabel
from pytestqt.qtbot import QtBot

from mailbrief.app import create_application
from mailbrief.ui.main_window import MainWindow

pytestmark = pytest.mark.skipif(
    (
        sys.platform == "darwin"
        and not os.environ.get("DISPLAY")
        and not os.environ.get("MACOS_GUI_AVAILABLE")
    ),
    reason="Requires active macOS GUI WindowServer session",
)


def test_create_application_reuses_qapplication(qapp: QApplication) -> None:
    assert create_application([]) is qapp
    assert qapp.applicationName() == "MailBrief"


def test_main_window_has_mvp_placeholder(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    label = window.centralWidget()

    assert window.windowTitle() == "MailBrief"
    assert isinstance(label, QLabel)
    assert label.text() == "MailBrief MVP foundation is ready."
