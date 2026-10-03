"""Desktop diagnostics never persist exception payloads or unrecognized codes."""

from datetime import UTC, datetime
from pathlib import Path

from mailbrief.domain.briefs import BriefRunResult, BriefStatus
from mailbrief.domain.digests import DigestCoverage, SyncResult, SyncStatus
from mailbrief.ui.diagnostics import (
    configure_logging,
    error_guidance,
    log_automatic_run,
    log_failure,
    logger,
)


def test_rotating_log_contains_only_types_and_known_codes(tmp_path: Path) -> None:
    handler = configure_logging(tmp_path)
    try:
        log_failure(RuntimeError("SECRET email body token"))
        error_guidance("AI_AUTH_FAILED")
        error_guidance("SECRET provider payload")
        handler.flush()
        content = (tmp_path / "desktop.log").read_text()
        assert "RuntimeError" in content
        assert "AI_AUTH_FAILED" in content
        assert "UNKNOWN" in content
        assert "SECRET" not in content
        assert "Traceback" not in content
        assert handler.maxBytes == 128 * 1024
        assert handler.backupCount == 2
    finally:
        logger.removeHandler(handler)
        handler.close()


def result(**fields: object) -> BriefRunResult:
    sync = SyncResult(
        account_id="owner@example.com",
        range_start_utc=datetime(2026, 9, 30, tzinfo=UTC),
        range_end_utc=datetime(2026, 10, 1, tzinfo=UTC),
        status=SyncStatus.COMPLETE,
        page_count=1,
        message_count=1,
    )
    return BriefRunResult(sync=sync, **fields)


def test_an_automatic_run_is_logged_as_counts_only(tmp_path: Path) -> None:
    handler = configure_logging(tmp_path)
    try:
        log_automatic_run(result(status=BriefStatus.READY_FOR_REVIEW, ready=4))
        coverage = DigestCoverage(
            sync_complete=True, shortlisted=5, analyzed=2, reused=1, failed=0, skipped=0, deferred=2
        )
        log_automatic_run(
            result(
                status=BriefStatus.ANALYSIS_FAILED,
                coverage=coverage,
                deferred=2,
                ai_calls=3,
                error_code="AI_RATE_LIMITED",
            )
        )
        # A run that wrote no brief because a carried message failed still shows its requests.
        failed = DigestCoverage(
            sync_complete=True, shortlisted=5, analyzed=2, reused=0, failed=3, skipped=0
        )
        log_automatic_run(
            result(
                status=BriefStatus.READY_FOR_REVIEW,
                unrefreshed=3,
                ready=1,
                coverage=failed,
                ai_calls=2,
                error_code="AI_RATE_LIMITED",
            )
        )
        handler.flush()
        lines = (tmp_path / "desktop.log").read_text().splitlines()
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert lines[0].endswith(
        "automatic run: status=ready_for_review ready=4 unrefreshed=0 analyzed=0 deferred=0 "
        "ai_requests=0"
    )
    assert lines[1].endswith(
        "automatic run: status=analysis_failed ready=0 unrefreshed=0 analyzed=2 deferred=2 "
        "ai_requests=3"
    )
    assert lines[2].endswith(
        "automatic run: status=ready_for_review ready=1 unrefreshed=3 analyzed=2 deferred=0 "
        "ai_requests=2"
    )
    assert "AI_RATE_LIMITED" not in "".join(lines)  # Codes go through error_guidance, once.
