"""Brief contracts: outcomes, the transmission preview and run results."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mailbrief.domain.briefs import (
    SENT_FIELDS,
    AnalysisOutcome,
    BriefRunResult,
    BriefStatus,
    TransmissionPreview,
)
from mailbrief.domain.digests import (
    DailyDigest,
    DigestStatus,
    SyncResult,
    SyncStatus,
)
from tests.factories import TEST_GENERATED_AT, TEST_LOCAL_DATE

SYNC = SyncResult(
    account_id="owner@example.com",
    range_start_utc=datetime(2026, 9, 4, 4, 0, tzinfo=UTC),
    range_end_utc=datetime(2026, 9, 5, 4, 0, tzinfo=UTC),
    status=SyncStatus.COMPLETE,
    page_count=1,
    message_count=0,
)
DIGEST = DailyDigest(
    account_id="owner@example.com",
    local_date=TEST_LOCAL_DATE,
    timezone_name="America/Toronto",
    generated_at_utc=TEST_GENERATED_AT,
    status=DigestStatus.EMPTY,
)


def preview(**overrides: object) -> TransmissionPreview:
    values: dict[str, object] = {
        "provider_name": "openai",
        "model_name": "model-1",
        "message_count": 3,
        "truncated_count": 1,
        "reused_count": 2,
        "first_use": True,
    }
    values.update(overrides)
    return TransmissionPreview.model_validate(values)


def test_outcome_values_are_lowercase() -> None:
    assert [outcome.value for outcome in AnalysisOutcome] == [
        "analyzed",
        "reused",
        "failed",
        "skipped",
    ]


def test_preview_lists_the_sent_fields_by_default() -> None:
    assert preview().fields == SENT_FIELDS
    assert "8,000 characters" in SENT_FIELDS[-1]


@pytest.mark.parametrize(
    "overrides",
    [{"message_count": 0}, {"truncated_count": 4}, {"reused_count": -1}, {"provider_name": ""}],
    ids=["nothing-to-send", "too-many-cut", "negative-reuse", "no-provider"],
)
def test_invalid_previews_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        preview(**overrides)


def test_a_saved_run_carries_its_digest() -> None:
    result = BriefRunResult(status=BriefStatus.SAVED, sync=SYNC, digest=DIGEST)

    assert result.digest == DIGEST
    assert [status.value for status in BriefStatus] == [
        "saved",
        "sync_failed",
        "cancelled",
        "consent_declined",
        "analysis_failed",
    ]


@pytest.mark.parametrize(
    ("status", "digest"),
    [(BriefStatus.SAVED, None), (BriefStatus.ANALYSIS_FAILED, DIGEST)],
    ids=["saved-without-digest", "digest-without-save"],
)
def test_a_digest_is_present_exactly_when_saved(
    status: BriefStatus, digest: DailyDigest | None
) -> None:
    with pytest.raises(ValidationError, match="present exactly when"):
        BriefRunResult(status=status, sync=SYNC, digest=digest)
