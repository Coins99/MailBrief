"""Tests for daily digest and synchronization models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mailbrief.domain.digests import (
    DailyDigest,
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
