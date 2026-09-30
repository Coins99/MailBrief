"""When an automatic run is due: an injected clock, ticks called by hand, no real waiting."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pytestqt.qtbot import QtBot

from mailbrief.ui.scheduler import TICK_MS, RefreshScheduler

START = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, by: timedelta) -> None:
        self.now += by


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def scheduler(qtbot: QtBot, clock: Clock) -> Iterator[RefreshScheduler]:
    result = RefreshScheduler(clock=clock)
    emitted: list[datetime] = []
    result.due.connect(lambda: emitted.append(clock.now))
    result.emitted = emitted  # type: ignore[attr-defined]
    yield result
    result.stop()


def next_due(scheduler: RefreshScheduler) -> datetime | None:
    """Read fresh each time: mypy would narrow a property across calls that change it."""
    return scheduler.next_due


def running(scheduler: RefreshScheduler) -> bool:
    return scheduler.active


def fired(scheduler: RefreshScheduler) -> list[datetime]:
    return scheduler.emitted  # type: ignore[attr-defined, no-any-return]


def test_nothing_is_due_until_an_interval_is_chosen(
    scheduler: RefreshScheduler, clock: Clock
) -> None:
    assert next_due(scheduler) is None and not running(scheduler)
    scheduler.configure(False, None)
    clock.advance(timedelta(days=3))

    scheduler.tick()

    assert fired(scheduler) == [] and next_due(scheduler) is None and not running(scheduler)


def test_the_timer_ticks_once_a_minute(scheduler: RefreshScheduler) -> None:
    assert scheduler._timer.interval() == TICK_MS == 60_000


def test_a_run_is_due_one_interval_after_the_settings_and_then_every_interval(
    scheduler: RefreshScheduler, clock: Clock
) -> None:
    scheduler.configure(False, 60)
    assert next_due(scheduler) == START + HOUR and running(scheduler)

    clock.advance(timedelta(minutes=59, seconds=59))
    scheduler.tick()
    assert fired(scheduler) == []  # Not yet.

    clock.advance(timedelta(seconds=1))
    scheduler.tick()
    scheduler.tick()  # A second tick at once has nothing more to say.
    assert fired(scheduler) == [START + HOUR]
    assert next_due(scheduler) == START + 2 * HOUR and running(scheduler)

    clock.advance(HOUR)
    scheduler.tick()
    assert fired(scheduler) == [START + HOUR, START + 2 * HOUR]


@pytest.mark.parametrize("minutes", [60, 120, 240])
def test_each_offered_interval_is_honoured(
    scheduler: RefreshScheduler, clock: Clock, minutes: int
) -> None:
    scheduler.configure(False, minutes)
    clock.advance(timedelta(minutes=minutes - 1))
    scheduler.tick()
    assert fired(scheduler) == []

    clock.advance(timedelta(minutes=1))
    scheduler.tick()

    assert len(fired(scheduler)) == 1
    assert next_due(scheduler) == clock.now + timedelta(minutes=minutes)


def test_any_number_of_missed_intervals_gives_exactly_one_run(
    scheduler: RefreshScheduler, clock: Clock
) -> None:
    scheduler.configure(False, 60)

    clock.advance(timedelta(hours=7, minutes=30))  # The computer slept through seven of them.
    scheduler.tick()
    scheduler.tick()
    scheduler.tick()

    assert len(fired(scheduler)) == 1
    # The next run is an interval after the one just made, not after the missed ones.
    assert next_due(scheduler) == START + timedelta(hours=8, minutes=30)
    clock.advance(timedelta(minutes=59))
    scheduler.tick()
    assert len(fired(scheduler)) == 1
    clock.advance(timedelta(minutes=1))
    scheduler.tick()
    assert len(fired(scheduler)) == 2


def test_a_finished_run_pushes_the_next_one_an_interval_after_it(
    scheduler: RefreshScheduler, clock: Clock
) -> None:
    scheduler.configure(False, 120)
    finished = START + timedelta(minutes=90)

    scheduler.note_run(finished)

    assert next_due(scheduler) == finished + 2 * HOUR
    clock.now = START + 2 * HOUR  # The original time passes without a run.
    scheduler.tick()
    assert fired(scheduler) == []
    clock.now = finished + 2 * HOUR
    scheduler.tick()
    assert len(fired(scheduler)) == 1


def test_a_finished_run_with_no_interval_schedules_nothing(
    scheduler: RefreshScheduler,
) -> None:
    scheduler.configure(True, None)

    scheduler.note_run(START)

    assert next_due(scheduler) is None and not running(scheduler)


def test_a_retry_is_due_again_at_the_next_tick(scheduler: RefreshScheduler, clock: Clock) -> None:
    scheduler.configure(False, 240)

    scheduler.retry_soon()
    assert next_due(scheduler) == clock.now and running(scheduler)
    clock.advance(timedelta(seconds=60))
    scheduler.tick()

    assert len(fired(scheduler)) == 1
    assert next_due(scheduler) == clock.now + 4 * HOUR  # Back on the interval.


def test_a_retry_works_with_no_interval_and_then_stops_the_timer(
    scheduler: RefreshScheduler, clock: Clock
) -> None:
    scheduler.configure(True, None)

    scheduler.retry_soon()
    assert running(scheduler)
    scheduler.tick()

    assert len(fired(scheduler)) == 1
    assert next_due(scheduler) is None and not running(scheduler)


def test_a_retry_asked_for_while_handling_due_wins(
    scheduler: RefreshScheduler, clock: Clock
) -> None:
    scheduler.configure(False, 60)
    busy = [True]
    scheduler.due.connect(lambda: scheduler.retry_soon() if busy[0] else None)

    clock.advance(HOUR)
    scheduler.tick()
    assert next_due(scheduler) == clock.now  # Due again, not an hour from now.

    busy[0] = False
    clock.advance(timedelta(seconds=60))
    scheduler.tick()
    assert len(fired(scheduler)) == 2  # The first try, then the retry a tick later.
    assert next_due(scheduler) == clock.now + HOUR


def test_launching_runs_once_when_asked_to(scheduler: RefreshScheduler, clock: Clock) -> None:
    scheduler.configure(True, None)

    scheduler.launched()
    scheduler.launched()

    assert fired(scheduler) == [START]
    assert next_due(scheduler) is None and not running(scheduler)


def test_launching_with_an_interval_runs_once_and_keeps_the_schedule(
    scheduler: RefreshScheduler, clock: Clock
) -> None:
    scheduler.configure(True, 60)
    clock.advance(timedelta(minutes=10))

    scheduler.launched()

    assert fired(scheduler) == [clock.now]
    assert next_due(scheduler) == clock.now + HOUR and running(scheduler)


def test_launching_does_nothing_unless_asked_to_and_never_later(
    scheduler: RefreshScheduler,
) -> None:
    scheduler.configure(False, 60)
    scheduler.launched()
    assert fired(scheduler) == []

    scheduler.configure(True, 60)  # Turned on after this start: next start, not this one.
    scheduler.launched()

    assert fired(scheduler) == []


def test_configuring_again_replaces_the_schedule_and_a_waiting_retry(
    scheduler: RefreshScheduler, clock: Clock
) -> None:
    scheduler.configure(False, 60)
    scheduler.retry_soon()

    scheduler.configure(False, 240)

    assert next_due(scheduler) == clock.now + 4 * HOUR
    scheduler.tick()
    assert fired(scheduler) == []
    scheduler.configure(False, None)  # Off again: nothing scheduled, the timer stops.
    assert next_due(scheduler) is None and not running(scheduler)


def test_a_clock_set_back_does_not_leave_a_run_hours_away(
    scheduler: RefreshScheduler, clock: Clock
) -> None:
    scheduler.configure(False, 60)
    clock.advance(-timedelta(days=2))

    scheduler.tick()

    assert fired(scheduler) == []
    assert next_due(scheduler) == clock.now + HOUR
    clock.advance(HOUR)
    scheduler.tick()
    assert len(fired(scheduler)) == 1


def test_the_timer_drives_tick(scheduler: RefreshScheduler, clock: Clock) -> None:
    scheduler.configure(False, 60)
    clock.advance(HOUR)

    scheduler._timer.timeout.emit()

    assert len(fired(scheduler)) == 1


def test_stopping_ends_everything_for_good(scheduler: RefreshScheduler, clock: Clock) -> None:
    scheduler.configure(True, 60)
    assert running(scheduler)

    scheduler.stop()

    assert not running(scheduler) and next_due(scheduler) is None
    clock.advance(timedelta(days=1))
    scheduler.tick()
    scheduler.launched()
    scheduler.retry_soon()
    scheduler.note_run(clock.now)
    scheduler.configure(True, 60)
    scheduler.tick()
    assert fired(scheduler) == [] and not running(scheduler)
