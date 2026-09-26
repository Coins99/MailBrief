"""Tests for AI request, response and result contracts."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from mailbrief.domain.analysis import (
    ANALYSIS_SCHEMA_VERSION,
    AIUsage,
    AnalysisCandidate,
    AnalysisCategory,
    AnalysisProblem,
    AnalysisRequest,
    AnalysisResponse,
    DeadlinePrecision,
    MessageAnalysis,
)
from mailbrief.domain.messages import EmailContact
from tests.factories import make_analysis

MARKER = "SYNTHETIC-PRIVATE-MARKER-4f1c"
FRIDAY = date(2026, 9, 4)
FRIDAY_5PM_TORONTO = datetime(2026, 9, 4, 21, 0, tzinfo=UTC)
NO_DEADLINE: dict[str, object] = {
    "deadline_text": None,
    "deadline_precision": DeadlinePrecision.NONE,
    "deadline_date": None,
    "deadline_at_utc": None,
    "deadline_timezone": None,
}
UNRESOLVED: dict[str, object] = {
    **NO_DEADLINE,
    "deadline_text": "ASAP",
    "deadline_precision": DeadlinePrecision.UNRESOLVED,
}
DATE_ONLY: dict[str, object] = {
    **NO_DEADLINE,
    "deadline_text": "Friday",
    "deadline_precision": DeadlinePrecision.DATE,
    "deadline_date": FRIDAY,
    "deadline_timezone": "America/Toronto",
}
EXACT: dict[str, object] = {
    "deadline_text": "Friday 5 PM",
    "deadline_precision": DeadlinePrecision.DATETIME,
    "deadline_date": FRIDAY,
    "deadline_at_utc": FRIDAY_5PM_TORONTO,
    "deadline_timezone": "America/Toronto",
}


def make_request(**overrides: object) -> AnalysisRequest:
    values: dict[str, object] = {
        "message_key": "local-1",
        "subject": "Subject",
        "sender": EmailContact(address="alex@example.com"),
        "received_at_utc": datetime(2026, 8, 31, 14, 30, tzinfo=UTC),
        "timezone_name": "America/Toronto",
        "body_text": "Body",
    }
    values.update(overrides)
    return AnalysisRequest.model_validate(values)


def candidate_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "message_key": "local-1",
        "category": "deadline",
        "summary": "Submit the report.",
        "action_required": True,
        "action_text": "Submit the report.",
        "deadline_text": "Friday 5 PM",
        "deadline_date": "2026-09-04",
        "deadline_time": "17:00",
        "stated_timezone": None,
        "confidence": 0.8,
        "evidence": "Please submit the report by Friday 5 PM.",
    }
    values.update(overrides)
    return values


def test_schema_version_is_two() -> None:
    assert ANALYSIS_SCHEMA_VERSION == "2"


def test_message_analysis_round_trips_as_json() -> None:
    analysis = make_analysis()

    restored = MessageAnalysis.model_validate_json(analysis.model_dump_json())

    assert restored == analysis
    assert restored.deadline_precision is DeadlinePrecision.DATETIME


def test_action_text_is_required_for_required_action() -> None:
    with pytest.raises(ValidationError, match="action_text is required"):
        make_analysis(action_text=None)


def test_deadline_category_requires_deadline_text() -> None:
    with pytest.raises(ValidationError, match="deadline analysis requires deadline_text"):
        make_analysis(
            category=AnalysisCategory.DEADLINE,
            action_required=False,
            action_text=None,
            **NO_DEADLINE,
        )


@pytest.mark.parametrize(
    "deadline",
    [NO_DEADLINE, UNRESOLVED, DATE_ONLY, EXACT],
    ids=["none", "unresolved", "date", "datetime"],
)
def test_consistent_deadline_precisions_are_accepted(deadline: dict[str, object]) -> None:
    analysis = make_analysis(**deadline)

    assert analysis.deadline_precision == deadline["deadline_precision"]


@pytest.mark.parametrize(
    "deadline",
    [
        {**NO_DEADLINE, "deadline_text": "Friday"},
        {**NO_DEADLINE, "deadline_date": FRIDAY},
        {**NO_DEADLINE, "deadline_at_utc": FRIDAY_5PM_TORONTO},
        {**NO_DEADLINE, "deadline_timezone": "America/Toronto"},
        {**UNRESOLVED, "deadline_text": None},
        {**UNRESOLVED, "deadline_date": FRIDAY},
        {**UNRESOLVED, "deadline_at_utc": FRIDAY_5PM_TORONTO},
        {**UNRESOLVED, "deadline_timezone": "America/Toronto"},
        {**DATE_ONLY, "deadline_text": None},
        {**DATE_ONLY, "deadline_date": None},
        {**DATE_ONLY, "deadline_timezone": None},
        {**DATE_ONLY, "deadline_at_utc": FRIDAY_5PM_TORONTO},
        {**EXACT, "deadline_text": None},
        {**EXACT, "deadline_date": None},
        {**EXACT, "deadline_at_utc": None},
        {**EXACT, "deadline_timezone": None},
    ],
    ids=[
        "none-with-text",
        "none-with-date",
        "none-with-instant",
        "none-with-zone",
        "unresolved-without-text",
        "unresolved-with-date",
        "unresolved-with-instant",
        "unresolved-with-zone",
        "date-without-text",
        "date-without-date",
        "date-without-zone",
        "date-with-instant",
        "datetime-without-text",
        "datetime-without-date",
        "datetime-without-instant",
        "datetime-without-zone",
    ],
)
def test_inconsistent_deadline_precisions_are_rejected(deadline: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_analysis(**deadline)


def test_datetime_deadline_must_fall_on_its_local_date() -> None:
    with pytest.raises(ValidationError, match="must fall on deadline_date"):
        make_analysis(deadline_date=date(2026, 9, 5))


def test_datetime_deadline_date_uses_the_deadline_zone() -> None:
    late_evening_in_toronto = datetime(2026, 9, 5, 2, 0, tzinfo=UTC)

    analysis = make_analysis(deadline_at_utc=late_evening_in_toronto)

    assert analysis.deadline_date == FRIDAY
    with pytest.raises(ValidationError, match="must fall on deadline_date"):
        make_analysis(deadline_at_utc=late_evening_in_toronto, deadline_timezone="UTC")


@pytest.mark.parametrize("zone", ["Mars/Olympus_Mons", "America", "../etc/localtime"])
def test_unloadable_time_zones_are_rejected(zone: str) -> None:
    with pytest.raises(ValidationError, match="loadable IANA name"):
        make_analysis(deadline_timezone=zone)
    with pytest.raises(ValidationError, match="loadable IANA name"):
        make_request(timezone_name=zone)


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidence_must_be_in_closed_unit_interval(confidence: float) -> None:
    with pytest.raises(ValidationError):
        make_analysis(confidence=confidence)


def test_analysis_request_enforces_minimized_body_limit() -> None:
    with pytest.raises(ValidationError, match="at most 8000 characters"):
        make_request(body_text="x" * 8_001)


def test_analysis_request_normalizes_timestamp_and_defaults() -> None:
    request = make_request(received_at_utc="2026-08-31T10:30:00-04:00")

    assert request.received_at_utc == datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
    assert request.body_truncated is False
    assert request.timezone_name == "America/Toronto"


def test_analysis_request_errors_never_echo_the_body() -> None:
    with pytest.raises(ValidationError) as too_long:
        make_request(body_text=MARKER + "x" * 8_001)
    with pytest.raises(ValidationError) as missing:
        AnalysisRequest.model_validate({"message_key": "local-1", "body_text": MARKER})

    for error in (too_long.value, missing.value):
        assert MARKER not in str(error)
        assert MARKER not in repr(error)


def test_candidate_accepts_unbounded_provider_text() -> None:
    candidate = AnalysisCandidate.model_validate(candidate_values(summary="s" * 5_000))

    assert len(candidate.summary) == 5_000
    assert candidate.category is AnalysisCategory.DEADLINE
    assert candidate.deadline_time == "17:00"


@pytest.mark.parametrize("field", sorted(AnalysisCandidate.model_fields))
def test_candidate_fields_are_all_required(field: str) -> None:
    values = candidate_values()
    del values[field]

    with pytest.raises(ValidationError):
        AnalysisCandidate.model_validate(values)


def test_candidate_errors_never_echo_provider_text() -> None:
    invalid_type = candidate_values(summary=[MARKER], evidence=MARKER)
    missing_field = candidate_values(summary=MARKER, evidence=MARKER)
    del missing_field["message_key"]

    for values in (invalid_type, missing_field):
        with pytest.raises(ValidationError) as caught:
            AnalysisCandidate.model_validate(values)
        assert MARKER not in str(caught.value)
        assert MARKER not in repr(caught.value)


def test_response_with_a_problem_has_no_candidates() -> None:
    candidate = AnalysisCandidate.model_validate(candidate_values(summary=MARKER))

    with pytest.raises(ValidationError, match="cannot contain candidates") as caught:
        AnalysisResponse(candidates=(candidate,), problem=AnalysisProblem.REFUSED)

    assert MARKER not in str(caught.value)
    refused = AnalysisResponse(problem=AnalysisProblem.REFUSED)
    assert refused.candidates == ()
    answered = AnalysisResponse(candidates=(candidate,), usage=AIUsage(input_tokens=10))
    assert answered.problem is None


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
def test_usage_counts_cannot_be_negative(field: str) -> None:
    with pytest.raises(ValidationError):
        AIUsage.model_validate({field: -1})
