"""Brief contracts: outcomes, the transmission preview and run results."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mailbrief.domain.briefs import (
    AUTO_SEND_LIMIT_MAX,
    SENT_FIELDS,
    AnalysisOutcome,
    AutoSendPermission,
    AutoSendStatus,
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
        "body_character_limit": 4_000,
        "privacy_notice": "The provider keeps nothing.",
    }
    values.update(overrides)
    return TransmissionPreview.model_validate(values)


def test_outcome_values_are_lowercase() -> None:
    assert [outcome.value for outcome in AnalysisOutcome] == [
        "analyzed",
        "reused",
        "failed",
        "skipped",
        "deferred",
    ]


def test_preview_fields_state_the_applied_body_limit() -> None:
    assert preview().fields == (
        *SENT_FIELDS,
        "plain-text body, cut to at most 4,000 characters",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"message_count": 0},
        {"truncated_count": 4},
        {"reused_count": -1},
        {"provider_name": ""},
        {"body_character_limit": 0},
        {"body_character_limit": 8_001},
        {"privacy_notice": ""},
        {"privacy_notice": "x" * 501},
    ],
    ids=[
        "nothing-to-send",
        "too-many-cut",
        "negative-reuse",
        "no-provider",
        "no-body",
        "body-over-limit",
        "no-notice",
        "notice-too-long",
    ],
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
        "ready_for_review",
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


def test_a_run_that_only_checked_reports_what_is_ready_and_has_no_digest() -> None:
    result = BriefRunResult(status=BriefStatus.READY_FOR_REVIEW, sync=SYNC, ready=4)

    assert (result.ready, result.deferred, result.digest) == (4, 0, None)
    assert (BriefRunResult(status=BriefStatus.SAVED, sync=SYNC, digest=DIGEST).ready) == 0
    with pytest.raises(ValidationError):  # A brief is saved, never merely "ready".
        BriefRunResult(status=BriefStatus.READY_FOR_REVIEW, sync=SYNC, digest=DIGEST)


@pytest.mark.parametrize("field", ["ready", "deferred"])
def test_ready_and_deferred_counts_cannot_be_negative(field: str) -> None:
    with pytest.raises(ValidationError):
        BriefRunResult(status=BriefStatus.READY_FOR_REVIEW, sync=SYNC, **{field: -1})


def test_a_saved_run_counts_what_it_deferred() -> None:
    result = BriefRunResult(status=BriefStatus.SAVED, sync=SYNC, digest=DIGEST, deferred=2)

    assert result.deferred == 2


@pytest.mark.parametrize("limit", [0, 1, AUTO_SEND_LIMIT_MAX])
def test_a_permission_holds_zero_to_ten_messages(limit: int) -> None:
    permission = AutoSendPermission(account_email="me@example.com", limit=limit)

    assert (permission.limit, permission.granted_at_utc) == (limit, None)


@pytest.mark.parametrize("limit", [-1, AUTO_SEND_LIMIT_MAX + 1])
def test_a_permission_outside_zero_to_ten_is_refused(limit: int) -> None:
    with pytest.raises(ValidationError):
        AutoSendPermission(account_email="me@example.com", limit=limit)


def test_a_permission_keeps_its_time_in_utc_and_a_status_its_disclosure() -> None:
    from datetime import timedelta, timezone

    toronto = timezone(timedelta(hours=-4))
    status = AutoSendStatus(
        account_email="me@example.com",
        limit=2,
        granted_at_utc=datetime(2026, 9, 30, 8, tzinfo=toronto),
        disclosure=preview(message_count=2, first_use=False),
    )

    assert status.granted_at_utc == datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert status.disclosure.message_count == 2
    with pytest.raises(ValidationError):  # A status needs its disclosure.
        AutoSendStatus.model_validate({"account_email": "me@example.com", "limit": 2})
