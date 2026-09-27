"""Desktop application entry point."""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from mailbrief.paths import AppPaths, configure_qt_identity
from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.runtime import DesktopRuntime


def create_application(arguments: Sequence[str] | None = None) -> QApplication:
    """Create or return the process-wide Qt application."""
    configure_qt_identity()
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing

    application = QApplication(list(arguments) if arguments is not None else sys.argv)
    return application


async def run_desktop() -> None:
    """Keep the loop alive until pending work and resources have shut down."""
    paths = AppPaths.from_qt()
    window = MainWindow(DesktopRuntime(paths.database_path))
    closed = asyncio.Event()
    window.closing.connect(closed.set)
    window.show()
    window.start(window.initialize)
    try:
        await closed.wait()
    finally:
        await window.shutdown()


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
            loop.run_until_complete(run_desktop())
    return 0
