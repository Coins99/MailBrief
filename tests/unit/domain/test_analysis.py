"""Tests for AI request and result contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mailbrief.domain.analysis import AnalysisCategory, AnalysisRequest, MessageAnalysis
from mailbrief.domain.messages import EmailContact, RankReason
from tests.factories import make_analysis


def test_message_analysis_round_trips_as_json() -> None:
    analysis = make_analysis()

    restored = MessageAnalysis.model_validate_json(analysis.model_dump_json())

    assert restored == analysis


def test_action_text_is_required_for_required_action() -> None:
    with pytest.raises(ValidationError, match="action_text is required"):
        make_analysis(action_text=None)


def test_deadline_category_requires_deadline_information() -> None:
    with pytest.raises(ValidationError, match="deadline analysis requires"):
        make_analysis(
            category=AnalysisCategory.DEADLINE,
            action_required=False,
            action_text=None,
            deadline_text=None,
            deadline_at_utc=None,
        )


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidence_must_be_in_closed_unit_interval(confidence: float) -> None:
    with pytest.raises(ValidationError):
        make_analysis(confidence=confidence)


def test_analysis_request_enforces_minimized_body_limit() -> None:
    with pytest.raises(ValidationError, match="at most 8000 characters"):
        AnalysisRequest(
            message_key="local-1",
            subject="Subject",
            sender=EmailContact(address="alex@example.com"),
            received_at_utc=datetime(2026, 8, 31, 14, 30, tzinfo=UTC),
            ranking_reasons=(RankReason.UNREAD,),
            body_text="x" * 8_001,
        )


def test_analysis_request_normalizes_timestamp() -> None:
    request = AnalysisRequest(
        message_key="local-1",
        subject="Subject",
        sender=EmailContact(address="alex@example.com"),
        received_at_utc="2026-08-31T10:30:00-04:00",
        body_text="Body",
    )

    assert request.received_at_utc == datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
