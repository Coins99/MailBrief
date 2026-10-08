"""Act on the desktop's MainWindow once it is visible, without ever waiting forever."""

import time
from collections.abc import Callable

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from mailbrief.ui.main_window import MainWindow

NEVER_VISIBLE = "MainWindow never became visible."
NEVER_READY = "MainWindow was visible, but the test's action never completed."


def visible_main_window() -> MainWindow | None:
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, MainWindow) and widget.isVisible():
            return widget
    return None


class WindowWait:
    """Calls ``act`` with the visible MainWindow, retrying every 10 ms until it returns
    True. If it hasn't returned True within ``timeout`` seconds of the first attempt,
    whether or not a window was visible, it records the failure and calls ``quit``
    (``QApplication.quit`` by default, so ``app.main()`` returns); ``run`` then fails the
    test.
    """

    def __init__(
        self,
        act: Callable[[MainWindow], bool],
        *,
        timeout: float = 10.0,
        find: Callable[[], MainWindow | None] = visible_main_window,
        quit: Callable[[], None] = QApplication.quit,
    ) -> None:
        self._act = act
        self._timeout = timeout
        self._find = find
        self._quit = quit
        self._started: float | None = None
        self._seen = False
        self.failure: str | None = None

    @property
    def failed(self) -> bool:
        return self.failure is not None

    def start(self, delay_ms: int = 100) -> None:
        QTimer.singleShot(delay_ms, self._attempt)

    def _attempt(self) -> None:
        now = time.monotonic()
        if self._started is None:
            self._started = now
        window = self._find()
        if window is not None:
            self._seen = True
            if self._act(window):
                return
        if now - self._started >= self._timeout:
            self.failure = NEVER_READY if self._seen else NEVER_VISIBLE
            self._quit()
            return
        QTimer.singleShot(10, self._attempt)

    def run(self, main: Callable[[], int]) -> int:
        """Run ``main``, failing the test if the wait gave up."""
        try:
            result = main()
        except Exception:
            if self.failure is not None:
                pytest.fail(self.failure)
            raise
        if self.failure is not None:
            pytest.fail(self.failure)
        return result
