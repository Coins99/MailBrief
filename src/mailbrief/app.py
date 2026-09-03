"""Desktop application entry point."""

import sys
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication

from mailbrief.paths import configure_qt_identity
from mailbrief.ui.main_window import MainWindow


def create_application(arguments: Sequence[str] | None = None) -> QApplication:
    """Create or return the process-wide Qt application."""
    configure_qt_identity()
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing

    application = QApplication(list(arguments) if arguments is not None else sys.argv)
    return application


def main() -> int:
    """Launch the MailBrief desktop application."""
    application = create_application()
    window = MainWindow()
    window.show()
    return application.exec()
