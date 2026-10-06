"""The MainWindow wait never hangs a test: it quits Qt and fails after its timeout."""

import time

import pytest
from PySide6.QtWidgets import QApplication

from mailbrief.ui.main_window import MainWindow
from tests.ui.window_wait import NEVER_VISIBLE, WindowWait


def test_a_window_that_never_appears_quits_and_fails(qapp: QApplication) -> None:
    acted: list[MainWindow] = []

    def act(window: MainWindow) -> bool:
        acted.append(window)
        return True

    wait = WindowWait(act, timeout=0.05, find=lambda: None)
    wait.start(0)
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match=NEVER_VISIBLE):
        wait.run(qapp.exec)
    assert wait.failed and acted == []
    assert time.monotonic() - started < 5


def test_a_failure_wins_over_the_error_main_raises_after_the_quit(qapp: QApplication) -> None:
    wait = WindowWait(lambda window: True, timeout=0.0, find=lambda: None)
    wait.start(0)

    def main() -> int:
        qapp.exec()
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
