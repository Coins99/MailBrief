"""Smoke tests for the Qt application shell."""

from PySide6.QtWidgets import QApplication, QLabel
from pytestqt.qtbot import QtBot

from mailbrief.app import create_application
from mailbrief.ui.main_window import MainWindow


def test_create_application_reuses_qapplication(qapp: QApplication) -> None:
    assert create_application([]) is qapp


def test_main_window_has_mvp_placeholder(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    label = window.centralWidget()

    assert window.windowTitle() == "MailBrief"
    assert isinstance(label, QLabel)
    assert label.text() == "MailBrief MVP foundation is ready."
