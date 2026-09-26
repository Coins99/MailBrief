"""Tests for daily digest and synchronization models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mailbrief.domain.analysis import DeadlinePrecision
from mailbrief.domain.digests import (
    DailyDigest,
    DigestCoverage,
    DigestStatus,
    SyncProgress,
    SyncResult,
    SyncStage,
    SyncStatus,
)
from tests.factories import TEST_GENERATED_AT, TEST_LOCAL_DATE, make_digest_item


def test_daily_digest_round_trips_as_json() -> None:
    digest = DailyDigest(
        account_id="account-1",
        local_date=TEST_LOCAL_DATE,
        timezone_name="America/Toronto",
        generated_at_utc=TEST_GENERATED_AT,
        status=DigestStatus.COMPLETE,
        items=(make_digest_item(),),
    )

    restored = DailyDigest.model_validate_json(digest.model_dump_json())

    assert restored == digest


def test_empty_digest_accepts_no_items() -> None:
    digest = DailyDigest(
        account_id="account-1",
        local_date=TEST_LOCAL_DATE,
        timezone_name="America/Toronto",
        generated_at_utc=TEST_GENERATED_AT,
        status=DigestStatus.EMPTY,
    )

    assert digest.items == ()


def test_empty_digest_rejects_items() -> None:
    with pytest.raises(ValidationError, match="empty digest cannot contain items"):
        DailyDigest(
            account_id="account-1",
            local_date=TEST_LOCAL_DATE,
            timezone_name="America/Toronto",
            generated_at_utc=TEST_GENERATED_AT,
            status=DigestStatus.EMPTY,
            items=(make_digest_item(),),
        )


def test_nonempty_digest_requires_items() -> None:
    with pytest.raises(ValidationError, match="must contain at least one item"):
        DailyDigest(
            account_id="account-1",
            local_date=TEST_LOCAL_DATE,
            timezone_name="America/Toronto",
            generated_at_utc=TEST_GENERATED_AT,
            status=DigestStatus.COMPLETE,
        )


def test_digest_positions_must_be_unique_and_contiguous() -> None:
    with pytest.raises(ValidationError, match="positions must be unique"):
        DailyDigest(
            account_id="account-1",
            local_date=TEST_LOCAL_DATE,
            timezone_name="America/Toronto",
            generated_at_utc=TEST_GENERATED_AT,
            status=DigestStatus.COMPLETE,
            items=(make_digest_item(), make_digest_item(message_key="local-2")),
        )

    with pytest.raises(ValidationError, match="contiguous"):
        DailyDigest(
            account_id="account-1",
            local_date=TEST_LOCAL_DATE,
            timezone_name="America/Toronto",
            generated_at_utc=TEST_GENERATED_AT,
            status=DigestStatus.COMPLETE,
            items=(make_digest_item(position=1),),
        )


def test_digest_rejects_the_same_message_twice() -> None:
    with pytest.raises(ValidationError, match="message may appear only once"):
        DailyDigest(
            account_id="account-1",
            local_date=TEST_LOCAL_DATE,
            timezone_name="America/Toronto",
            generated_at_utc=TEST_GENERATED_AT,
            status=DigestStatus.COMPLETE,
            items=(make_digest_item(), make_digest_item(position=1)),
        )


def test_sync_result_validates_range_and_failure_code() -> None:
    start = datetime(2026, 8, 31, tzinfo=UTC)

    with pytest.raises(ValidationError, match="later than"):
        SyncResult(
            account_id="account-1",
            range_start_utc=start,
            range_end_utc=start,
            status=SyncStatus.COMPLETE,
            page_count=0,
            message_count=0,
        )

    with pytest.raises(ValidationError, match="requires an error_code"):
        SyncResult(
            account_id="account-1",
            range_start_utc=start,
            range_end_utc=datetime(2026, 9, 1, tzinfo=UTC),
            status=SyncStatus.FAILED,
            page_count=0,
            message_count=0,
        )


def test_sync_progress_rejects_negative_counts() -> None:
    with pytest.raises(ValidationError):
        SyncProgress(stage=SyncStage.FETCHING, pages_fetched=-1)


def make_coverage(**overrides: object) -> DigestCoverage:
    values: dict[str, object] = {
        "sync_complete": True,
        "shortlisted": 5,
        "analyzed": 2,
        "reused": 1,
        "failed": 1,
        "skipped": 1,
        "input_tokens": 1_200,
        "output_tokens": 300,
        "ai_provider": "openai",
        "ai_model": "model-1",
    }
    values.update(overrides)
    return DigestCoverage.model_validate(values)


def test_digest_item_deadline_details_default_to_none() -> None:
    item = make_digest_item()

    assert item.deadline_precision is DeadlinePrecision.NONE
    assert item.deadline_text is None
    assert item.deadline_date is None
    assert item.evidence is None


def test_digest_item_repr_leaves_out_email_text() -> None:
    item = make_digest_item(deadline_text="by Friday", evidence="Please approve the proposal")

    text = repr(item)

    for private in ("Approval needed", "Approve the proposal", "by Friday", "Please approve"):
        assert private not in text
    assert "local-1" in text


def test_digest_with_coverage_round_trips_as_json() -> None:
    digest = DailyDigest(
        account_id="account-1",
        local_date=TEST_LOCAL_DATE,
        timezone_name="America/Toronto",
        generated_at_utc=TEST_GENERATED_AT,
        status=DigestStatus.PARTIAL,
        items=(make_digest_item(),),
        coverage=make_coverage(),
    )

    restored = DailyDigest.model_validate_json(digest.model_dump_json())

    assert restored == digest
    assert restored.coverage == make_coverage()


@pytest.mark.parametrize("shortlisted", [4, 6])
def test_coverage_counts_must_add_up_to_the_shortlist(shortlisted: int) -> None:
    with pytest.raises(ValidationError, match="must add up"):
        make_coverage(shortlisted=shortlisted)


@pytest.mark.parametrize(
    "field",
    ["shortlisted", "analyzed", "reused", "failed", "skipped", "input_tokens", "output_tokens"],
)
def test_coverage_counts_cannot_be_negative(field: str) -> None:
    with pytest.raises(ValidationError):
        make_coverage(**{field: -1})


def test_coverage_allows_unknown_usage_and_provider() -> None:
    coverage = make_coverage(
        shortlisted=0,
        analyzed=0,
        reused=0,
        failed=0,
        skipped=0,
        input_tokens=None,
        output_tokens=None,
        ai_provider=None,
        ai_model=None,
    )

    assert coverage.shortlisted == 0
    assert coverage.ai_provider is None
