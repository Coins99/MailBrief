"""Deadline resolution rules D1-D7, DST edges and stated zones."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from mailbrief.domain.analysis import AnalysisCandidate, AnalysisRequest, DeadlinePrecision
from mailbrief.domain.messages import EmailContact
from mailbrief.services.deadlines import InvalidDeadlineError, ResolvedDeadline, resolve_deadline

ZONE = "America/Toronto"
BODY = (
    "Please approve the budget by Friday 5 PM. Reply ASAP. The board meets 8 March at 2:30, "
    "the audit call is 1 November at 1:30, and London wants it by 5 PM London time or 5 PM PT."
)
SEPTEMBER_2 = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
NO_DEADLINE = ResolvedDeadline(None, DeadlinePrecision.NONE, None, None, None)


def request(received: datetime = SEPTEMBER_2, **overrides: object) -> AnalysisRequest:
    values: dict[str, object] = {
        "message_key": "0000abcd",
        "subject": "Budget approval",
        "sender": EmailContact(address="alex@example.com"),
        "received_at_utc": received,
        "timezone_name": ZONE,
        "body_text": BODY,
    }
    values.update(overrides)
    return AnalysisRequest.model_validate(values)


def candidate(**overrides: object) -> AnalysisCandidate:
    values: dict[str, object] = {
        "message_key": "0000abcd",
        "category": "deadline",
        "summary": "Approve the budget.",
        "action_required": False,
        "action_text": None,
        "deadline_text": None,
        "deadline_date": None,
        "deadline_time": None,
        "stated_timezone": None,
        "confidence": 0.9,
        "evidence": "Please approve the budget",
    }
    values.update(overrides)
    return AnalysisCandidate.model_validate(values)


def friday(**overrides: object) -> AnalysisCandidate:
    values: dict[str, object] = {"deadline_text": "Friday 5 PM", "deadline_date": "2026-09-04"}
    return candidate(**(values | overrides))


@pytest.mark.parametrize("text", [None, "", "   "])
def test_d1_no_text_means_no_deadline_and_ignores_the_stated_zone(text: str | None) -> None:
    resolved = resolve_deadline(
        candidate(deadline_text=text, stated_timezone="Europe/London"), request()
    )

    assert resolved == NO_DEADLINE


@pytest.mark.parametrize("fields", [{"deadline_date": "2026-09-04"}, {"deadline_time": "17:00"}])
def test_d1_a_date_or_time_without_text_is_invalid(fields: dict[str, object]) -> None:
    with pytest.raises(InvalidDeadlineError):
        resolve_deadline(candidate(**fields), request())


def test_d2_a_phrase_that_is_not_in_the_email_is_invalid() -> None:
    with pytest.raises(InvalidDeadlineError) as caught:
        resolve_deadline(candidate(deadline_text="next Tuesday at noon"), request())

    assert "Tuesday" not in str(caught.value)


def test_d2_text_over_500_characters_is_invalid_even_when_quoted() -> None:
    long_phrase = "y" * 501

    with pytest.raises(InvalidDeadlineError):
        resolve_deadline(
            candidate(deadline_text=long_phrase), request(body_text=f"{long_phrase} end")
        )


def test_d2_d3_text_quoted_from_the_subject_without_a_date_is_unresolved() -> None:
    resolved = resolve_deadline(candidate(deadline_text="budget APPROVAL"), request())

    assert resolved == ResolvedDeadline(
        "budget APPROVAL", DeadlinePrecision.UNRESOLVED, None, None, None
    )


def test_d3_blank_date_and_time_leave_an_unresolved_phrase() -> None:
    resolved = resolve_deadline(
        candidate(deadline_text="ASAP", deadline_date=" ", deadline_time=""), request()
    )

    assert resolved == ResolvedDeadline("ASAP", DeadlinePrecision.UNRESOLVED, None, None, None)


def test_d3_a_time_without_a_date_is_invalid() -> None:
    with pytest.raises(InvalidDeadlineError):
        resolve_deadline(candidate(deadline_text="ASAP", deadline_time="17:00"), request())


@pytest.mark.parametrize(
    "raw", ["2026/09/04", "Sept 4", "2026-9-4", "20260904", "2026-02-30", "２026-09-04"]
)
def test_d4_dates_must_be_real_iso_days(raw: str) -> None:
    with pytest.raises(InvalidDeadlineError):
        resolve_deadline(candidate(deadline_text="Friday 5 PM", deadline_date=raw), request())


@pytest.mark.parametrize(
    ("raw", "valid"),
    [("2026-08-01", False), ("2026-08-02", True), ("2027-09-03", True), ("2027-09-04", False)],
)
def test_d4_dates_must_fall_in_the_window_around_the_local_received_day(
    raw: str, valid: bool
) -> None:
    late_evening_in_toronto = datetime(2026, 9, 3, 2, 0, tzinfo=UTC)  # 2 September, 22:00
    deadline = candidate(deadline_text="Friday 5 PM", deadline_date=raw)

    if valid:
        resolved = resolve_deadline(deadline, request(late_evening_in_toronto))
        assert resolved.date == date.fromisoformat(raw)
    else:
        with pytest.raises(InvalidDeadlineError):
            resolve_deadline(deadline, request(late_evening_in_toronto))


def test_d5_a_date_without_a_time_is_a_day_in_the_user_zone() -> None:
    resolved = resolve_deadline(friday(stated_timezone="Europe/London"), request())

    assert resolved == ResolvedDeadline(
        "Friday 5 PM", DeadlinePrecision.DATE, date(2026, 9, 4), None, ZONE
    )


@pytest.mark.parametrize("raw", ["24:00", "23:60", "12:00:60", "7pm", "7:00", "17", "17:00 PM"])
def test_d6_times_must_be_hh_mm_within_the_day(raw: str) -> None:
    with pytest.raises(InvalidDeadlineError):
        resolve_deadline(friday(deadline_time=raw), request())


def test_d6_d7_an_exact_time_resolves_in_the_user_zone_without_seconds() -> None:
    resolved = resolve_deadline(friday(deadline_time="17:00:45"), request())

    assert resolved == ResolvedDeadline(
        "Friday 5 PM",
        DeadlinePrecision.DATETIME,
        date(2026, 9, 4),
        datetime(2026, 9, 4, 21, 0, tzinfo=UTC),
        ZONE,
    )


def test_d6_a_stated_iana_zone_is_used() -> None:
    resolved = resolve_deadline(
        candidate(
            deadline_text="5 PM London time",
            deadline_date="2026-09-04",
            deadline_time="17:00",
            stated_timezone="Europe/London",
        ),
        request(),
    )

    assert resolved.precision is DeadlinePrecision.DATETIME
    assert resolved.at_utc == datetime(2026, 9, 4, 16, 0, tzinfo=UTC)
    assert resolved.timezone == "Europe/London"


@pytest.mark.parametrize("zone", ["PT", "Pacific Time"])
def test_d6_an_unloadable_stated_zone_keeps_only_the_day(zone: str) -> None:
    resolved = resolve_deadline(
        candidate(
            deadline_text="5 PM PT",
            deadline_date="2026-09-04",
            deadline_time="17:00",
            stated_timezone=zone,
        ),
        request(),
    )

    assert resolved == ResolvedDeadline(
        "5 PM PT", DeadlinePrecision.DATE, date(2026, 9, 4), None, ZONE
    )


def test_d7_a_time_in_the_spring_forward_gap_stays_consistent() -> None:
    resolved = resolve_deadline(
        candidate(
            deadline_text="8 March at 2:30", deadline_date="2026-03-08", deadline_time="02:30"
        ),
        request(datetime(2026, 3, 6, 15, 0, tzinfo=UTC)),
    )

    assert resolved.precision is DeadlinePrecision.DATETIME
    assert resolved.at_utc is not None
    assert resolved.at_utc == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert resolved.at_utc.astimezone(ZoneInfo(ZONE)).date() == resolved.date == date(2026, 3, 8)


def test_d7_an_ambiguous_fall_back_time_uses_its_first_occurrence() -> None:
    resolved = resolve_deadline(
        candidate(
            deadline_text="1 November at 1:30", deadline_date="2026-11-01", deadline_time="01:30"
        ),
        request(datetime(2026, 10, 30, 15, 0, tzinfo=UTC)),
    )

    assert resolved.at_utc == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert resolved.date == date(2026, 11, 1)


def test_invalid_deadline_errors_are_static_value_errors() -> None:
    with pytest.raises(ValueError) as caught:
        resolve_deadline(friday(deadline_date="not a date"), request())

    assert isinstance(caught.value, InvalidDeadlineError)
    assert "not a date" not in str(caught.value)
