"""Email synchronization service coordinating provider retrieval and database persistence."""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mailbrief.domain.digests import SyncProgress, SyncResult, SyncStage, SyncStatus
from mailbrief.infra.http_retry import RetryTracker, current_retry_tracker
from mailbrief.ports.email_provider import EmailProvider
from mailbrief.ports.errors import (
    AuthenticationRequiredError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from mailbrief.services.calendar import DayWindow
from mailbrief.storage.repositories import (
    AccountRepository,
    MessageRepository,
    SyncRunRepository,
)

logger = logging.getLogger(__name__)

MAX_RUN_WORK_SECONDS = 180.0
MAX_SINGLE_WAIT_SECONDS = 60.0
MAX_TOTAL_WAIT_SECONDS = 120.0


def _sanitize_error_code(exc: Exception) -> str:
    """Map exception types to closed, non-sensitive audit error codes."""
    if isinstance(exc, AuthenticationRequiredError):
        return "AUTH_REQUIRED"
    if isinstance(exc, ProviderPermissionError):
        return "PERMISSION_DENIED"
    if isinstance(exc, ProviderRateLimitError):
        return "RATE_LIMITED"
    if isinstance(exc, ProviderResponseError):
        return "PROVIDER_RESPONSE_ERROR"
    if isinstance(exc, ProviderError):
        return "PROVIDER_ERROR"
    return "UNKNOWN_ERROR"


class SyncService:
    """Coordinates email retrieval from EmailProvider and atomic SQLite storage."""

    def __init__(
        self,
        *,
        provider: EmailProvider,
        message_repo: MessageRepository,
        sync_run_repo: SyncRunRepository,
        account_repo: AccountRepository,
        session: Any = None,
    ) -> None:
        self._provider = provider
        self._message_repo = message_repo
        self._sync_run_repo = sync_run_repo
        self._account_repo = account_repo
        self._session = session or getattr(message_repo, "_session", None)

    async def _commit(self) -> None:
        if self._session:
            await self._session.commit()

    async def _rollback(self) -> None:
        if self._session:
            await self._session.rollback()

    async def sync_day(
        self,
        *,
        account_id: int,
        account_identity: str,
        window: DayWindow,
        progress: Callable[[SyncProgress], None] | None = None,
        cancel: asyncio.Event | None = None,
    ) -> SyncResult:
        """Fetch every page for the local-day window, committing each page as it arrives.

        Only a complete, error-free enumeration reconciles Inbox membership and advances
        the account's last-sync time. Provider rate-limit waits of up to
        MAX_SINGLE_WAIT_SECONDS are honored by resuming from the last committed page.
        """
        tracker = RetryTracker()
        token = current_retry_tracker.set(tracker)
        try:
            return await self._sync_day(
                account_id=account_id,
                account_identity=account_identity,
                window=window,
                progress=progress,
                cancel=cancel,
                tracker=tracker,
            )
        finally:
            current_retry_tracker.reset(token)

    async def _sync_day(
        self,
        *,
        account_id: int,
        account_identity: str,
        window: DayWindow,
        progress: Callable[[SyncProgress], None] | None,
        cancel: asyncio.Event | None,
        tracker: RetryTracker,
    ) -> SyncResult:
        sync_run = await self._sync_run_repo.create_sync_run(
            account_id=account_id,
            range_start_utc=window.start_utc,
            range_end_utc=window.end_utc,
            status="started",
        )
        await self._commit()
        sync_run_id = sync_run.id

        pages_count = 0
        messages_count = 0
        failed_count = 0
        seen_ids: set[str] = set()
        local_wait_seconds = 0.0
        continuation: str | None = None

        def emit_progress() -> None:
            if progress is None:
                return
            try:
                progress(
                    SyncProgress(
                        stage=SyncStage.FETCHING,
                        pages_fetched=pages_count,
                        messages_fetched=messages_count,
                        failed_message_count=failed_count,
                    )
                )
            except Exception as exc:
                logger.warning("Progress callback raised an exception: %s", exc)

        async def finish(status: SyncStatus, error_code: str | None) -> SyncResult:
            await self._sync_run_repo.update_sync_run(
                sync_run_id,
                status=status,
                page_count=pages_count,
                message_count=messages_count,
                failed_message_count=failed_count,
                error_code=error_code,
                completed=True,
            )
            await self._commit()
            return SyncResult(
                account_id=account_identity,
                range_start_utc=window.start_utc,
                range_end_utc=window.end_utc,
                status=status,
                page_count=pages_count,
                message_count=messages_count,
                failed_message_count=failed_count,
                error_code=error_code,
            )

        work_start = time.monotonic()

        def work_budget_exceeded() -> bool:
            waited = tracker.sleep_seconds + local_wait_seconds
            return (time.monotonic() - work_start) - waited > MAX_RUN_WORK_SECONDS

        emit_progress()
        try:
            while True:
                if work_budget_exceeded():
                    logger.warning("Sync run exceeded its active-work budget before a page")
                    status = SyncStatus.PARTIAL if pages_count > 0 else SyncStatus.FAILED
                    return await finish(status, "SYNC_RUN_TIMEOUT")

                try:
                    pages = self._provider.iter_message_pages(
                        range_start_utc=window.start_utc,
                        range_end_utc=window.end_utc,
                        continuation=continuation,
                    )
                    async for page in pages:
                        if cancel is not None and cancel.is_set():
                            return await finish(SyncStatus.CANCELLED, None)

                        await self._message_repo.upsert_messages(account_id, page.messages)
                        pages_count += 1
                        messages_count += len(page.messages)
                        failed_count += page.failed_message_count
                        seen_ids.update(
                            m.provider_message_id for m in page.messages if m.is_in_inbox
                        )
                        continuation = page.continuation

                        await self._sync_run_repo.update_sync_run(
                            sync_run_id,
                            page_count=pages_count,
                            message_count=messages_count,
                            failed_message_count=failed_count,
                        )
                        await self._commit()
                        emit_progress()

                        if continuation is not None and work_budget_exceeded():
                            logger.warning("Sync run exceeded its active-work budget")
                            return await finish(SyncStatus.PARTIAL, "SYNC_RUN_TIMEOUT")
                    break

                except ProviderRateLimitError as exc:
                    if tracker.sleep_seconds == 0.0 and exc.accumulated_sleep_seconds > 0.0:
                        local_wait_seconds += exc.accumulated_sleep_seconds
                    total_wait_seconds = tracker.sleep_seconds + local_wait_seconds
                    wait_delay = exc.retry_after_seconds
                    if (
                        wait_delay is None
                        or wait_delay > MAX_SINGLE_WAIT_SECONDS
                        or (total_wait_seconds + wait_delay) > MAX_TOTAL_WAIT_SECONDS
                    ):
                        error_code = exc.provider_error_code or "RATE_LIMITED"
                        logger.warning(
                            "Rate-limit wait ceiling exceeded (wait=%s, waited=%.1fs): %s",
                            wait_delay,
                            total_wait_seconds,
                            error_code,
                        )
                        status = SyncStatus.PARTIAL if pages_count > 0 else SyncStatus.FAILED
                        return await finish(status, error_code)

                    logger.info("Honoring provider rate-limit wait of %.1fs", wait_delay)
                    emit_progress()
                    await asyncio.sleep(wait_delay)
                    local_wait_seconds += wait_delay
                    # The loop resumes from `continuation`, the last committed page.

        except asyncio.CancelledError:
            await self._rollback()
            await finish(SyncStatus.CANCELLED, None)
            raise

        except Exception as exc:
            await self._rollback()
            error_code = _sanitize_error_code(exc)
            logger.error("Synchronization failed for account %s: %s", account_id, error_code)
            status = SyncStatus.PARTIAL if pages_count > 0 else SyncStatus.FAILED
            return await finish(status, error_code)

        if cancel is not None and cancel.is_set():
            return await finish(SyncStatus.CANCELLED, None)
        if failed_count:
            return await finish(SyncStatus.PARTIAL, "METADATA_ITEMS_FAILED")

        await self._message_repo.reconcile_inbox(
            account_id, window.start_utc, window.end_utc, seen_ids
        )
        await self._account_repo.update_last_sync(account_id, datetime.now(UTC))
        return await finish(SyncStatus.COMPLETE, None)
