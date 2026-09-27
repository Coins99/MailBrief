"""Smoke tests for the Qt application shell."""

import os
import sys

import pytest
from PySide6.QtWidgets import QApplication

from mailbrief.app import create_application

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
