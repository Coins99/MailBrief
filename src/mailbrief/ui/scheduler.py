"""When an automatic run is due, while MailBrief is open (ADR 0017).

The scheduler only says *when*: the window decides whether a run can start. It lives and
dies with the window. One timer ticks, once a minute by default, and compares the wall clock
with the next due time, so a computer that slept for hours gets exactly one run on waking,
however many intervals it missed. Nothing is scheduled with the operating system, there is
no tray or background process, and ``stop()`` ends the timer.
"""

from collections.abc import Callable
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QTimer, Signal

from mailbrief.domain.common import utc_now

TICK_MS = 60_000  # A minute: how often due time is checked, and how long a retry waits.


class RefreshScheduler(QObject):
    """``due()`` says an automatic run is wanted now.

    It is emitted when MailBrief starts (once, if the owner asked for that and ``launched()``
    is called), and whenever the next due time has passed. Every emission sets the next due
    time one interval on, so any number of missed intervals gives one run. The clock is
    injected, and ``tick()`` is what the timer calls, so nothing here needs real waiting.
    """

    due = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        tick_ms: int = TICK_MS,
    ) -> None:
        super().__init__(parent)
        self._clock = clock
        self._on_launch = False
        self._interval: timedelta | None = None
        self._next_due: datetime | None = None
        self._launched = False
        self._stopped = False
        self._timer = QTimer(self)
        self._timer.setInterval(tick_ms)
        self._timer.timeout.connect(self.tick)

    @property
    def active(self) -> bool:
        """Whether the timer is running: only while a run is waiting to become due."""
        return self._timer.isActive()

    @property
    def next_due(self) -> datetime | None:
        """When the next run becomes due, or None when nothing is scheduled."""
        return self._next_due

    def configure(self, on_launch: bool, interval_minutes: int | None) -> None:
        """Use these settings, now: the first run is due one interval from now, or never.

        Changing them replaces whatever was scheduled, a waiting retry included.
        """
        if self._stopped:
            return
        self._on_launch = on_launch
        self._interval = timedelta(minutes=interval_minutes) if interval_minutes else None
        self._next_due = None if self._interval is None else self._clock() + self._interval
        self._arm()

    def launched(self) -> None:
        """MailBrief has started and can run: emit ``due`` once if asked to on launch.

        Later calls do nothing, so a run is never repeated for one start, whether or not
        the setting was on when the first call came.
        """
        if self._launched:
            return
        self._launched = True
        if self._on_launch and not self._stopped:
            self._fire()

    def note_run(self, at: datetime) -> None:
        """A brief or an automatic run finished at ``at``: the next one is an interval later."""
        if self._interval is not None and not self._stopped:
            self._next_due = at + self._interval
            self._arm()

    def retry_soon(self) -> None:
        """Something was in the way: be due again at the next tick."""
        if not self._stopped:
            self._next_due = self._clock()
            self._arm()

    def tick(self) -> None:
        """Emit ``due`` at most once, if the next due time has passed."""
        if self._next_due is None or self._stopped:
            return
        now = self._clock()
        if self._interval is not None and self._next_due - now > self._interval:
            # The clock was set back: don't wait for a time that is further off than an interval.
            self._next_due = now + self._interval
            return
        if now >= self._next_due:
            self._fire()

    def stop(self) -> None:
        """End for good: no timer runs and nothing is ever due again."""
        self._stopped = True
        self._next_due = None
        self._timer.stop()

    def _fire(self) -> None:
        # The next time is set first, so a retry asked for by a handler of ``due`` wins.
        self._next_due = None if self._interval is None else self._clock() + self._interval
        self._arm()
        self.due.emit()

    def _arm(self) -> None:
        """Run the timer exactly while something is scheduled."""
        if self._next_due is None or self._stopped:
            self._timer.stop()
        elif not self._timer.isActive():
            self._timer.start()
