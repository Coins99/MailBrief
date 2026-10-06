"""The MainWindow wait never hangs a test: it quits its loop and fails after its timeout.

These tests run a local QEventLoop and never start or quit the shared QApplication.
"""

import time
from collections.abc import Iterator
from typing import cast

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from mailbrief.ui.main_window import MainWindow
from tests.ui.window_wait import NEVER_READY, NEVER_VISIBLE, WindowWait

# Ends a loop the wait failed to quit, so a broken wait fails the test instead of hanging.
SAFETY_MS = 5000


@pytest.fixture
def loop(qapp: QApplication) -> Iterator[QEventLoop]:
    local = QEventLoop()
    QTimer.singleShot(SAFETY_MS, local.quit)
    yield local
    local.quit()


def test_a_window_that_never_appears_quits_and_fails(loop: QEventLoop) -> None:
    acted: list[MainWindow] = []

    def act(window: MainWindow) -> bool:
        acted.append(window)
        return True

    wait = WindowWait(act, timeout=0.05, find=lambda: None, quit=loop.quit)
    wait.start(0)
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match=NEVER_VISIBLE):
        wait.run(loop.exec)
    assert wait.failed and acted == []
    assert time.monotonic() - started < SAFETY_MS / 1000


def test_a_visible_window_whose_action_never_completes_still_times_out(
    loop: QEventLoop,
) -> None:
    window = cast(MainWindow, object())
    attempts: list[MainWindow] = []

    def act(found: MainWindow) -> bool:
        attempts.append(found)
        return False

    wait = WindowWait(act, timeout=0.05, find=lambda: window, quit=loop.quit)
    wait.start(0)
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match=NEVER_READY):
        wait.run(loop.exec)
    assert len(attempts) > 1 and all(found is window for found in attempts)
    assert time.monotonic() - started < SAFETY_MS / 1000


def test_a_failure_wins_over_the_error_main_raises_after_the_quit(loop: QEventLoop) -> None:
    wait = WindowWait(lambda window: True, timeout=0.0, find=lambda: None, quit=loop.quit)
    wait.start(0)

    def main() -> int:
        loop.exec()
        raise RuntimeError("Event loop stopped before Future completed.")

    with pytest.raises(pytest.fail.Exception, match=NEVER_VISIBLE):
        wait.run(main)


def test_an_error_without_a_timeout_is_not_hidden() -> None:
    wait = WindowWait(lambda window: True)

    def main() -> int:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        wait.run(main)
    assert wait.run(lambda: 0) == 0
