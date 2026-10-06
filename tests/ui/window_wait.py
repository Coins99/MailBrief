"""Act on the desktop's MainWindow once it is visible, without ever waiting forever."""

import time
from collections.abc import Callable

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from mailbrief.ui.main_window import MainWindow

NEVER_VISIBLE = "MainWindow never became visible."


def visible_main_window() -> MainWindow | None:
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, MainWindow) and widget.isVisible():
            return widget
    return None


class WindowWait:
    """Calls ``act`` with the visible MainWindow, retrying every 10 ms until it returns
    True. With no visible MainWindow for ``timeout`` seconds, measured from the first
    attempt, it records the failure and quits Qt so ``app.main()`` returns; ``run`` then
    fails the test.
    """

    def __init__(
        self,
        act: Callable[[MainWindow], bool],
        *,
        timeout: float = 10.0,
        find: Callable[[], MainWindow | None] = visible_main_window,
    ) -> None:
        self._act = act
        self._timeout = timeout
        self._find = find
        self._started: float | None = None
        self.failed = False

    def start(self, delay_ms: int = 100) -> None:
        QTimer.singleShot(delay_ms, self._attempt)

    def _attempt(self) -> None:
        now = time.monotonic()
        if self._started is None:
            self._started = now
        window = self._find()
        if window is not None:
            if self._act(window):
                return
        elif now - self._started >= self._timeout:
            self.failed = True
            QApplication.quit()
            return
        QTimer.singleShot(10, self._attempt)

    def run(self, main: Callable[[], int]) -> int:
        """Run ``main``, failing the test if the window never appeared."""
        try:
            result = main()
        except Exception:
            if self.failed:
                pytest.fail(NEVER_VISIBLE)
            raise
        if self.failed:
            pytest.fail(NEVER_VISIBLE)
        return result
