import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mailbrief.domain.digests import (
    SyncProgress,
    SyncResult,
    SyncStage,
    SyncStatus,
)
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
DEFAULT_RATE_LIMIT_DELAY = 5.0


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

    async def sync_day(
        self,
        *,
        account_id: int,
        account_identity: str,
        window: DayWindow,
        progress: Callable[[SyncProgress], None] | None = None,
        cancel: asyncio.Event | None = None,
    ) -> SyncResult:
        """Fetch all messages for the specified local day window and commit per page.

        Emits progress events and supports cancellation via asyncio.Event or task cancellation.
        """
        tracker = RetryTracker()
        token = current_retry_tracker.set(tracker)
        local_wait_seconds = 0.0

        try:
            sync_run = await self._sync_run_repo.create_sync_run(
                account_id=account_id,
                range_start_utc=window.start_utc,
                range_end_utc=window.end_utc,
                status="started",
            )
            if self._session:
                await self._session.commit()

            def emit_progress(p: SyncProgress) -> None:
                if progress:
                    try:
                        progress(p)
                    except Exception as exc:
                        logger.warning("Progress callback raised an exception: %s", exc)

            emit_progress(
                SyncProgress(stage=SyncStage.FETCHING, pages_fetched=0, messages_fetched=0)
            )  # noqa: E501

            pages_count = 0
            messages_count = 0
            current_continuation: str | None = None
            work_start = time.monotonic()
            honored_waits_count = 0

            try:
                while True:
                    total_wait_seconds = tracker.sleep_seconds + local_wait_seconds
                    active_work_elapsed = (time.monotonic() - work_start) - total_wait_seconds
                    if active_work_elapsed > MAX_RUN_WORK_SECONDS:
                        logger.warning(
                            "Sync run active work budget exceeded (%.1fs > %.1fs) for account %s",
                            active_work_elapsed,
                            MAX_RUN_WORK_SECONDS,
                            account_id,
                        )
                        error_code = "SYNC_RUN_TIMEOUT"
                        status = SyncStatus.PARTIAL if pages_count > 0 else SyncStatus.FAILED
                        await self._sync_run_repo.update_sync_run(
                            sync_run.id,
                            status=status,
                            page_count=pages_count,
                            message_count=messages_count,
                            error_code=error_code,
                            completed=True,
                        )
                        if self._session:
                            await self._session.commit()
                        return SyncResult(
                            account_id=account_identity,
                            range_start_utc=window.start_utc,
                            range_end_utc=window.end_utc,
                            status=status,
                            page_count=pages_count,
                            message_count=messages_count,
                            error_code=error_code,
                        )

                    try:
                        pages_iter = self._provider.iter_message_pages(
                            range_start_utc=window.start_utc,
                            range_end_utc=window.end_utc,
                            continuation=current_continuation,
                        )  # type: ignore[call-arg]

                        async for page in pages_iter:
                            if cancel and cancel.is_set():
                                await self._sync_run_repo.update_sync_run(
                                    sync_run.id,
                                    status=SyncStatus.CANCELLED,
                                    page_count=pages_count,
                                    message_count=messages_count,
                                    completed=True,
                                )
                                if self._session:
                                    await self._session.commit()
                                return SyncResult(
                                    account_id=account_identity,
                                    range_start_utc=window.start_utc,
                                    range_end_utc=window.end_utc,
                                    status=SyncStatus.CANCELLED,
                                    page_count=pages_count,
                                    message_count=messages_count,
                                )

                            await self._message_repo.upsert_messages(account_id, page.messages)
                            pages_count += 1
                            messages_count += len(page.messages)
                            current_continuation = page.continuation

                            await self._sync_run_repo.update_sync_run(
                                sync_run.id,
                                page_count=pages_count,
                                message_count=messages_count,
                            )
                            if self._session:
                                await self._session.commit()

                            emit_progress(
                                SyncProgress(
                                    stage=SyncStage.FETCHING,
                                    pages_fetched=pages_count,
                                    messages_fetched=messages_count,
                                )
                            )

                        # Finished all pages without error
                        break

                    except ProviderRateLimitError as exc:
                        sleep_from_exc = (
                            exc.accumulated_sleep_seconds if isinstance(exc, ProviderError) else 0.0
                        )
                        if tracker.sleep_seconds == 0.0 and sleep_from_exc > 0.0:
                            local_wait_seconds += sleep_from_exc

                        total_wait_seconds = tracker.sleep_seconds + local_wait_seconds
                        wait_delay = exc.retry_after_seconds
                        if (
                            wait_delay is None
                            or wait_delay > MAX_SINGLE_WAIT_SECONDS
                            or (total_wait_seconds + wait_delay) > MAX_TOTAL_WAIT_SECONDS
                        ):
                            error_code = exc.provider_error_code or "RATE_LIMITED"
                            logger.warning(
                                "Rate limit wait ceiling exceeded or no retry-after (wait=%s, total_slept=%.1fs, allowed_total=%.1fs): %s",  # noqa: E501
                                wait_delay,
                                total_wait_seconds,
                                MAX_TOTAL_WAIT_SECONDS,
                                error_code,
                            )
                            status = SyncStatus.PARTIAL if pages_count > 0 else SyncStatus.FAILED

                            await self._sync_run_repo.update_sync_run(
                                sync_run.id,
                                status=status,
                                page_count=pages_count,
                                message_count=messages_count,
                                error_code=error_code,
                                completed=True,
                            )
                            if self._session:
                                await self._session.commit()

                            return SyncResult(
                                account_id=account_identity,
                                range_start_utc=window.start_utc,
                                range_end_utc=window.end_utc,
                                status=status,
                                page_count=pages_count,
                                message_count=messages_count,
                                error_code=error_code,
                            )

                        honored_waits_count += 1
                        logger.info(
                            "Honoring provider rate limit wait of %.1fs (wait %d, accumulated=%.1fs)",  # noqa: E501
                            wait_delay,
                            honored_waits_count,
                            total_wait_seconds + wait_delay,
                        )
                        emit_progress(
                            SyncProgress(
                                stage=SyncStage.FETCHING,
                                pages_fetched=pages_count,
                                messages_fetched=messages_count,
                            )
                        )
                        await asyncio.sleep(wait_delay)
                        local_wait_seconds += wait_delay
                        # Loop restarts with continuation=current_continuation

                # Successfully retrieved and committed all pages
                await self._account_repo.update_last_sync(account_id, datetime.now(UTC))
                await self._sync_run_repo.update_sync_run(
                    sync_run.id,
                    status=SyncStatus.COMPLETE,
                    page_count=pages_count,
                    message_count=messages_count,
                    completed=True,
                )
                if self._session:
                    await self._session.commit()

                return SyncResult(
                    account_id=account_identity,
                    range_start_utc=window.start_utc,
                    range_end_utc=window.end_utc,
                    status=SyncStatus.COMPLETE,
                    page_count=pages_count,
                    message_count=messages_count,
                )

            except asyncio.CancelledError:
                await self._sync_run_repo.update_sync_run(
                    sync_run.id,
                    status=SyncStatus.CANCELLED,
                    page_count=pages_count,
                    message_count=messages_count,
                    completed=True,
                )
                if self._session:
                    await self._session.commit()
                raise

            except Exception as exc:
                error_code = _sanitize_error_code(exc)
                logger.error("Synchronization failed for account %s: %s", account_id, error_code)
                status = SyncStatus.PARTIAL if pages_count > 0 else SyncStatus.FAILED

                await self._sync_run_repo.update_sync_run(
                    sync_run.id,
                    status=status,
                    page_count=pages_count,
                    message_count=messages_count,
                    error_code=error_code,
                    completed=True,
                )
                if self._session:
                    await self._session.commit()

                return SyncResult(
                    account_id=account_identity,
                    range_start_utc=window.start_utc,
                    range_end_utc=window.end_utc,
                    status=status,
                    page_count=pages_count,
                    message_count=messages_count,
                    error_code=error_code,
                )
        finally:
            current_retry_tracker.reset(token)
