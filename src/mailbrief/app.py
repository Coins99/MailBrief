"""Desktop application entry point."""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QLocale, QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox
from qasync import QEventLoop

from mailbrief.paths import AppPaths, configure_qt_identity
from mailbrief.storage.recovery import BackupValidationError, restore_backup_holding_lock
from mailbrief.ui.diagnostics import configure_logging, log_failure, logger
from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.runtime import DesktopRuntime
from mailbrief.ui.theme import ThemeMode, apply_theme


def use_english_locale() -> None:
    """Make English the default locale, so Qt's own widgets (the calendar pop-ups' month
    and weekday names) read in English like the rest of the window, whatever the system
    language. A widget takes the default locale when it is created, so this runs before
    any widget exists."""
    QLocale.setDefault(QLocale(QLocale.Language.English))


def create_application(arguments: Sequence[str] | None = None) -> QApplication:
    """Create or return the process-wide Qt application."""
    configure_qt_identity()
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing

    application = QApplication(list(arguments) if arguments is not None else sys.argv)
    return application


async def run_desktop(theme_failure: Exception | None = None) -> None:
    """Keep the loop alive until pending work and resources have shut down.

    ``theme_failure`` is why the theme couldn't be applied, logged once desktop.log is open.
    """
    paths = AppPaths.from_qt()
    lock = QLockFile(str(paths.data_dir / "desktop.lock"))
    # Long-running instances must not be considered stale merely because of their age.
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        message = (
            "MailBrief is already running, or a MailBrief diagnostic command is using its "
            "data. Close it and retry."
            if lock.error() == QLockFile.LockError.LockFailedError
            else "MailBrief could not lock its data folder. Check folder access and retry."
        )
        QMessageBox.information(None, "MailBrief", message)
        return
    handler = None
    try:
        handler = configure_logging(paths.data_dir)
        if theme_failure is not None:
            log_failure(theme_failure)
        window = MainWindow(DesktopRuntime(paths.database_path), database_path=paths.database_path)
        closed = asyncio.Event()
        window.closing.connect(closed.set)
        window.show()
        window.start(window.initialize)
        try:
            await closed.wait()
        finally:
            await window.shutdown()
        if window.pending_restore is not None:
            # The folder lock stays held across shutdown and publication. No editor,
            # autosave task or pooled connection can write to the restored database.
            try:
                previous = await asyncio.to_thread(
                    restore_backup_holding_lock,
                    window.pending_restore[0],
                    paths.database_path,
                    replace=True,
                    expected=window.pending_restore[1],
                )
            except BackupValidationError as exc:
                # Fixed, safe text: archive content never appears in these messages.
                log_failure(exc)
                QMessageBox.warning(
                    None, "MailBrief recovery", f"{exc} The current database was kept."
                )
            except Exception as exc:
                log_failure(exc)
                QMessageBox.warning(
                    None,
                    "MailBrief recovery",
                    "Restore could not finish. "
                    "The current database was kept. Check free space, folder "
                    "access and other database clients, then retry.",
                )
            else:
                retained = f"Previous database: {previous}\n" if previous is not None else ""
                QMessageBox.information(
                    None,
                    "MailBrief recovery",
                    "Backup restored. "
                    f"{retained}"
                    "Relaunch MailBrief. Automatic AI permission is off.",
                )
    except Exception as exc:
        log_failure(exc)
        QMessageBox.warning(None, "MailBrief", "MailBrief could not start or close cleanly.")
    finally:
        if handler is not None:
            logger.removeHandler(handler)
            handler.close()
        lock.unlock()


def main(arguments: Sequence[str] | None = None) -> int:
    """Launch Qt and asyncio on one responsive event loop."""
    parser = argparse.ArgumentParser(prog="mailbrief")
    parser.add_argument(
        "--smoke-test-dir",
        type=Path,
        help="Run an offline package check using only this test directory, then exit.",
    )
    options = parser.parse_args(arguments)
    application = create_application([sys.argv[0]])
    use_english_locale()  # Before any widget: each takes the default locale when created.
    theme_failure: Exception | None = None
    try:
        apply_theme(application, ThemeMode.DARK)
    except Exception as exc:
        # The theme is cosmetic: apply_theme has put back what was there, with tokens that
        # suit it, so the app runs unthemed rather than not at all. desktop.log isn't open
        # yet, so the failure is logged once it is.
        theme_failure = exc
    application.setQuitOnLastWindowClosed(False)
    with QEventLoop(application) as loop:
        asyncio.set_event_loop(loop)
        if options.smoke_test_dir is not None:
            from mailbrief.ui.smoke import check_package

            try:
                loop.run_until_complete(check_package(options.smoke_test_dir))
            except Exception as exc:
                # Avoid an unattended frozen GUI error dialog and secret-bearing tracebacks.
                report = options.smoke_test_dir / "smoke-result.json"
                report.parent.mkdir(parents=True, exist_ok=True)
                failure = {"ok": False, "error": type(exc).__name__}
                if isinstance(exc, ModuleNotFoundError):
                    failure["missing_module"] = exc.name
                report.write_text(json.dumps(failure), encoding="utf-8")
                return 1
        else:
            loop.run_until_complete(run_desktop(theme_failure))
    return 0
