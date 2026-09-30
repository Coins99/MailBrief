"""Tests for accepted actions, their steps and sources, edits and suggestion views."""

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from mailbrief.domain.actions import (
    TARGET_REASON_TEXT,
    Action,
    ActionEdit,
    ActionFilter,
    ActionSource,
    ActionStatus,
    ActionStep,
    StepEdit,
    SuggestionState,
    SuggestionView,
    ThreadActivity,
)
from mailbrief.domain.analysis import ActionOwnership, DeadlinePrecision, TargetReason
from tests.factories import make_action, make_suggestion

MARKER = "SYNTHETIC-PRIVATE-MARKER-9b3a"
TORONTO = ZoneInfo("America/Toronto")
FRIDAY = date(2026, 9, 4)
COMPLETED_AT = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
PUBLIC_ID = "5f0d1a8e-2c3b-4e5f-8a9b-0c1d2e3f4a5b"
DATE_DEADLINE: dict[str, object] = {
    "deadline_text": "Friday",
    "deadline_precision": DeadlinePrecision.DATE,
    "deadline_date": FRIDAY,
    "deadline_timezone": "America/Toronto",
}
EXACT_DEADLINE: dict[str, object] = {
    "deadline_text": "Friday 5 PM",
    "deadline_precision": DeadlinePrecision.DATETIME,
    "deadline_date": FRIDAY,
    "deadline_at_utc": datetime(2026, 9, 4, 21, 0, tzinfo=UTC),
    "deadline_timezone": "America/Toronto",
}


def make_step(**overrides: object) -> ActionStep:
    values: dict[str, object] = {
        "step_id": 1,
        "position": 0,
        "text": "Read the proposal",
        "done": False,
    }
    values.update(overrides)
    return ActionStep.model_validate(values)


def make_source(**overrides: object) -> ActionSource:
    values: dict[str, object] = {
        "provider_message_id": "message-1",
        "subject": "Approval needed by Friday",
        "sender_address": "alex@example.com",
        "web_link": "https://mail.google.com/mail/u/0/#inbox/message-1",
        "received_at_utc": datetime(2026, 8, 31, 14, 30, tzinfo=UTC),
        "available": True,
        "in_inbox": True,
    }
    values.update(overrides)
    return ActionSource.model_validate(values)


def test_stored_values_are_stable() -> None:
    assert [status.value for status in ActionStatus] == ["open", "completed"]
    assert [state.value for state in SuggestionState] == ["pending", "accepted", "dismissed"]
    assert [view.value for view in ActionFilter] == ["open", "waiting", "completed"]


def test_every_target_reason_has_an_explanation() -> None:
    assert set(TARGET_REASON_TEXT) == set(TargetReason)
    assert TARGET_REASON_TEXT[TargetReason.WORKING_DAY_BEFORE] == (
        "one working day before the deadline"
    )


def test_an_action_round_trips_with_steps_and_sources() -> None:
    action = make_action(
        **EXACT_DEADLINE,
        suggested_target_date=date(2026, 9, 3),
        target_reason=TargetReason.WORKING_DAY_BEFORE,
        target_date=date(2026, 9, 2),
        notes="Ask Sam first.",
        evidence="Please approve the attached proposal",
        steps=(make_step(), make_step(step_id=2, position=1, done=True)),
        sources=(make_source(), make_source(provider_message_id="message-2", available=False)),
    )

    restored = Action.model_validate_json(action.model_dump_json())

    assert restored == action
    assert [step.step_id for step in restored.steps] == [1, 2]
    assert [source.available for source in restored.sources] == [True, False]


def test_a_completed_action_keeps_its_completion_time_in_utc() -> None:
    completed = make_action(
        status=ActionStatus.COMPLETED, completed_at_utc="2026-09-02T11:00:00-04:00"
    )

    assert completed.completed_at_utc == COMPLETED_AT
    assert make_action().completed_at_utc is None


@pytest.mark.parametrize(
    "fields",
    [
        {"status": ActionStatus.COMPLETED},
        {"status": ActionStatus.OPEN, "completed_at_utc": COMPLETED_AT},
    ],
    ids=["completed-without-time", "open-with-time"],
)
def test_completed_at_is_set_exactly_when_completed(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="exactly when the action is completed"):
        make_action(**fields)


def test_only_a_done_step_has_a_done_time() -> None:
    done = make_step(done=True, done_at_utc="2026-09-02T11:00:00-04:00")

    with pytest.raises(ValidationError, match="only a done step"):
        make_step(done_at_utc=COMPLETED_AT)

    assert done.done_at_utc == COMPLETED_AT
    assert make_step(done=True).done_at_utc is None


def test_timestamps_must_include_a_zone() -> None:
    naive = datetime(2026, 9, 2, 15, 0)

    for build in (
        lambda: make_action(created_at_utc=naive),
        lambda: make_step(done=True, done_at_utc=naive),
        lambda: make_source(received_at_utc=naive),
    ):
        with pytest.raises(ValidationError, match="must include a time zone"):
            build()


def test_a_date_deadline_is_overdue_from_the_next_local_midnight() -> None:
    action = make_action(**DATE_DEADLINE)
    midnight = datetime(2026, 9, 5, 4, 0, tzinfo=UTC)  # 00:00 on Saturday in Toronto.

    assert action.due_at_utc() == midnight
    assert not action.is_overdue(datetime(2026, 9, 4, 21, 0, tzinfo=UTC))
    assert not action.is_overdue(midnight - timedelta(microseconds=1))
    assert action.is_overdue(midnight)


def test_a_datetime_deadline_is_overdue_from_its_instant() -> None:
    action = make_action(**EXACT_DEADLINE)
    instant = datetime(2026, 9, 4, 21, 0, tzinfo=UTC)

    assert action.due_at_utc() == instant
    assert not action.is_overdue(instant - timedelta(seconds=1))
    assert action.is_overdue(instant)


def test_a_late_evening_deadline_is_overdue_from_its_local_minute_not_its_utc_day() -> None:
    """23:30 on Friday in Toronto is 03:30 on Saturday in UTC."""
    deadline = datetime(2026, 9, 4, 23, 30, tzinfo=TORONTO)
    action = make_action(
        deadline_text="Friday 11:30 PM",
        deadline_precision=DeadlinePrecision.DATETIME,
        deadline_date=FRIDAY,
        deadline_at_utc=deadline.astimezone(UTC),
        deadline_timezone="America/Toronto",
    )

    assert action.due_at_utc() == datetime(2026, 9, 5, 3, 30, tzinfo=UTC)
    assert not action.is_overdue(datetime(2026, 9, 4, 23, 29, tzinfo=TORONTO).astimezone(UTC))
    assert action.is_overdue(deadline.astimezone(UTC))


@pytest.mark.parametrize(
    "deadline",
    [{}, {"deadline_text": "ASAP", "deadline_precision": DeadlinePrecision.UNRESOLVED}],
    ids=["none", "unresolved"],
)
def test_an_action_without_a_day_is_never_overdue(deadline: dict[str, object]) -> None:
    action = make_action(**deadline)

    assert action.due_at_utc() is None
    assert not action.is_overdue(datetime(2099, 1, 1, tzinfo=UTC))


@pytest.mark.parametrize(
    "deadline",
    [
        {**DATE_DEADLINE, "deadline_timezone": None},
        {**DATE_DEADLINE, "deadline_timezone": "Mars/Olympus_Mons"},
        {**EXACT_DEADLINE, "deadline_date": date(2026, 9, 5)},
        {"deadline_text": "Friday"},
    ],
    ids=["date-without-zone", "unloadable-zone", "instant-off-its-day", "text-without-precision"],
)
def test_action_deadlines_are_checked(deadline: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_action(**deadline)


@pytest.mark.parametrize(
    ("created_at_utc", "today", "carried"),
    [
        (datetime(2026, 9, 4, 3, 59, tzinfo=UTC), date(2026, 9, 4), True),
        (datetime(2026, 9, 4, 4, 0, tzinfo=UTC), date(2026, 9, 4), False),
        # Spring forward on 8 March: 9 March starts at 04:00 UTC, not 05:00.
        (datetime(2026, 3, 9, 3, 59, tzinfo=UTC), date(2026, 3, 9), True),
        (datetime(2026, 3, 9, 4, 30, tzinfo=UTC), date(2026, 3, 9), False),
        # Fall back on 1 November: 2 November starts at 05:00 UTC, not 04:00.
        (datetime(2026, 11, 2, 4, 30, tzinfo=UTC), date(2026, 11, 2), True),
        (datetime(2026, 11, 2, 5, 0, tzinfo=UTC), date(2026, 11, 2), False),
    ],
    ids=[
        "just-before-midnight",
        "at-midnight",
        "before-midnight-after-spring-forward",
        "after-midnight-after-spring-forward",
        "before-midnight-after-fall-back",
        "at-midnight-after-fall-back",
    ],
)
def test_carried_over_compares_local_days(
    created_at_utc: datetime, today: date, carried: bool
) -> None:
    action = make_action(created_at_utc=created_at_utc, updated_at_utc=created_at_utc)

    assert action.carried_over(today, TORONTO) is carried


def test_an_action_created_later_the_same_day_is_not_carried_over() -> None:
    action = make_action()  # 10:30 on 31 August in Toronto.

    assert not action.carried_over(date(2026, 8, 31), TORONTO)
    assert action.carried_over(date(2026, 9, 1), TORONTO)


def test_carried_over_flips_exactly_at_local_midnight_on_the_fall_back_day() -> None:
    """1 November 2026 starts at 04:00 UTC, in daylight time; clocks fall back at 02:00."""
    midnight = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    fall_back_day = date(2026, 11, 1)

    def created(at: datetime) -> Action:
        return make_action(created_at_utc=at, updated_at_utc=at)

    assert midnight.astimezone(TORONTO).replace(tzinfo=None) == datetime(2026, 11, 1)
    assert created(midnight - timedelta(microseconds=1)).carried_over(fall_back_day, TORONTO)
    assert not created(midnight).carried_over(fall_back_day, TORONTO)


@pytest.mark.parametrize(
    "overrides",
    [
        {"public_id": "too-short"},
        {"public_id": PUBLIC_ID + "0"},
        {"title": " "},
        {"title": "t" * 201},
        {"notes": "n" * 10_001},
        {"evidence": "e" * 161},
        {"revision": 0},
        {"steps": tuple(make_step(step_id=index + 1, position=index) for index in range(31))},
    ],
    ids=[
        "short-id",
        "long-id",
        "blank-title",
        "long-title",
        "long-notes",
        "long-evidence",
        "revision-zero",
        "31-steps",
    ],
)
def test_action_bounds_are_enforced(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_action(**overrides)


def test_action_accepts_its_largest_values() -> None:
    action = make_action(
        title="t" * 200,
        notes="n" * 10_000,
        evidence="e" * 160,
        steps=tuple(
            make_step(step_id=index + 1, position=index, text="s" * 500) for index in range(30)
        ),
    )

    assert (len(action.title), len(action.notes), len(action.steps)) == (200, 10_000, 30)


@pytest.mark.parametrize(
    "overrides",
    [{"step_id": 0}, {"position": -1}, {"text": ""}, {"text": "s" * 501}],
    ids=["step-id-zero", "negative-position", "empty-text", "long-text"],
)
def test_step_bounds_are_enforced(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_step(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider_message_id": ""},
        {"subject": "s" * 999},
        {"sender_address": "ab"},
        {"web_link": "not a link"},
    ],
    ids=["empty-id", "long-subject", "short-address", "bad-link"],
)
def test_source_bounds_are_enforced(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_source(**overrides)


def test_a_source_may_not_know_its_inbox_state() -> None:
    source = make_source(in_inbox=None, available=False)

    assert (source.available, source.in_inbox) == (False, None)


def test_an_edit_carries_every_editable_field() -> None:
    edit = ActionEdit(
        title="Approve it",
        ownership=ActionOwnership.WAITING_FOR,
        effort=None,
        target_date=FRIDAY,
        notes="",
    )

    assert edit.ownership is ActionOwnership.WAITING_FOR
    with pytest.raises(ValidationError):
        ActionEdit.model_validate({"title": "Approve it", "ownership": "mine"})
    for bad in ({"title": "t" * 201}, {"notes": "n" * 10_001}, {"title": ""}):
        with pytest.raises(ValidationError):
            ActionEdit.model_validate({**edit.model_dump(), **bad})


def test_a_step_edit_without_an_id_is_new() -> None:
    new = StepEdit(step_id=None, text="Call Sam", done=False)
    existing = StepEdit(step_id=3, text="Call Sam", done=True)

    assert (new.step_id, existing.step_id) == (None, 3)
    for bad in ({"step_id": 0}, {"text": ""}, {"text": "s" * 501}):
        with pytest.raises(ValidationError):
            StepEdit.model_validate({**existing.model_dump(), **bad})


@pytest.mark.parametrize("state", [SuggestionState.PENDING, SuggestionState.DISMISSED])
def test_only_an_accepted_suggestion_links_to_an_action(state: SuggestionState) -> None:
    unlinked = SuggestionView(suggestion_id=1, suggestion=make_suggestion(), state=state)

    with pytest.raises(ValidationError, match="only an accepted suggestion"):
        SuggestionView(
            suggestion_id=1,
            suggestion=make_suggestion(),
            state=state,
            action_public_id=PUBLIC_ID,
        )

    assert unlinked.action_public_id is None


def test_an_accepted_suggestion_names_its_action() -> None:
    view = SuggestionView(
        suggestion_id=7,
        suggestion=make_suggestion(),
        state=SuggestionState.ACCEPTED,
        action_public_id=PUBLIC_ID,
    )

    assert view.action_public_id == PUBLIC_ID
    with pytest.raises(ValidationError):
        SuggestionView.model_validate({**view.model_dump(), "suggestion_id": 0})


def test_reprs_and_errors_leave_out_action_text() -> None:
    action = make_action(
        **{**EXACT_DEADLINE, "deadline_text": MARKER},
        title=MARKER,
        notes=MARKER,
        evidence=MARKER,
        steps=(make_step(text=MARKER),),
        sources=(make_source(subject=MARKER),),
    )
    edit = ActionEdit(
        title=MARKER, ownership=ActionOwnership.MINE, effort=None, target_date=None, notes=MARKER
    )
    step_edit = StepEdit(step_id=None, text=MARKER, done=False)

    with pytest.raises(ValidationError) as caught:
        make_action(title=MARKER * 10, notes=MARKER * 400, status=MARKER)

    for text in (repr(action), repr(edit), repr(step_edit), str(caught.value)):
        assert MARKER not in text


def test_thread_activity_sets_the_latest_message_exactly_when_something_is_new() -> None:
    at = datetime(2026, 9, 30, 12, tzinfo=UTC)
    quiet = ThreadActivity()
    assert not quiet.unseen
    replied = ThreadActivity(owner_replied_at_utc=at)
    assert replied.unseen and replied.new_messages == 0
    news = ThreadActivity(new_messages=2, latest_at_utc=at, latest_sender="Sam")
    assert news.unseen
    for broken in (
        {"new_messages": 1},
        {"new_messages": 1, "latest_at_utc": at},
        {"latest_at_utc": at, "latest_sender": "Sam"},
        {"new_messages": -1},
    ):
        with pytest.raises(ValidationError):
            ThreadActivity.model_validate(broken)


def test_an_action_s_seen_watermark_is_utc() -> None:
    toronto = timezone(timedelta(hours=-4))
    action = make_action(thread_seen_until_utc=datetime(2026, 9, 30, 8, tzinfo=toronto))
    assert action.thread_seen_until_utc == datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert action.thread is None
