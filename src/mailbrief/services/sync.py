"""Email synchronization service coordinating provider retrieval and database persistence."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from mailbrief.domain.digests import (
    SyncProgress,
    SyncResult,
    SyncStage,
    SyncStatus,
)
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
        provider: EmailProvider,
        message_repo: MessageRepository,
        sync_run_repo: SyncRunRepository,
        account_repo: AccountRepository,
    ) -> None:
        self._provider = provider
        self._message_repo = message_repo
        self._sync_run_repo = sync_run_repo
        self._account_repo = account_repo
        self._session = getattr(message_repo, "_session", None)

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
        sync_run = await self._sync_run_repo.create_sync_run(
            account_id=account_id,
            range_start_utc=window.start_utc,
            range_end_utc=window.end_utc,
            status="started",
        )
        if self._session:
            await self._session.commit()

        sync_run_id = sync_run.id

        def emit_progress(p: SyncProgress) -> None:
            if progress:
                try:
                    progress(p)
                except Exception as exc:
                    logger.warning("Progress callback raised an exception: %s", exc)

        emit_progress(SyncProgress(stage=SyncStage.FETCHING, pages_fetched=0, messages_fetched=0))

        pages_count = 0
        messages_count = 0
        failed_count = 0
        seen_ids: set[str] = set()

        try:
            pages_iter = self._provider.iter_message_pages(
                range_start_utc=window.start_utc,
                range_end_utc=window.end_utc,
            )

            async for page in pages_iter:
                if cancel and cancel.is_set():
                    await self._sync_run_repo.update_sync_run(
                        sync_run_id,
                        status=SyncStatus.CANCELLED,
                        page_count=pages_count,
                        message_count=messages_count,
                        failed_message_count=failed_count,
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
                        failed_message_count=failed_count,
                    )

                await self._message_repo.upsert_messages(account_id, page.messages)
                pages_count += 1
                messages_count += len(page.messages)
                failed_count += page.failed_message_count
                seen_ids.update(m.provider_message_id for m in page.messages if m.is_in_inbox)

                await self._sync_run_repo.update_sync_run(
                    sync_run_id,
                    page_count=pages_count,
                    message_count=messages_count,
                    failed_message_count=failed_count,
                )
                if self._session:
                    await self._session.commit()

                emit_progress(
                    SyncProgress(
                        stage=SyncStage.FETCHING,
                        pages_fetched=pages_count,
                        messages_fetched=messages_count,
                        failed_message_count=failed_count,
                    )
                )

            cancelled = cancel is not None and cancel.is_set()
            final_status = (
                SyncStatus.CANCELLED
                if cancelled
                else SyncStatus.PARTIAL
                if failed_count
                else SyncStatus.COMPLETE
            )
            error_code = "METADATA_ITEMS_FAILED" if failed_count else None
            if not failed_count and not cancelled:
                await self._message_repo.reconcile_inbox(
                    account_id, window.start_utc, window.end_utc, seen_ids
                )
                await self._account_repo.update_last_sync(account_id, datetime.now(UTC))
            await self._sync_run_repo.update_sync_run(
                sync_run_id,
                status=final_status,
                error_code=error_code,
                page_count=pages_count,
                message_count=messages_count,
                failed_message_count=failed_count,
                completed=True,
            )
            if self._session:
                await self._session.commit()

            return SyncResult(
                account_id=account_identity,
                range_start_utc=window.start_utc,
                range_end_utc=window.end_utc,
                status=final_status,
                error_code=error_code,
                page_count=pages_count,
                message_count=messages_count,
                failed_message_count=failed_count,
            )

        except asyncio.CancelledError:
            if self._session:
                await self._session.rollback()
            await self._sync_run_repo.update_sync_run(
                sync_run_id,
                status=SyncStatus.CANCELLED,
                page_count=pages_count,
                message_count=messages_count,
                failed_message_count=failed_count,
                completed=True,
            )
            if self._session:
                await self._session.commit()
            raise

        except Exception as exc:
            if self._session:
                await self._session.rollback()
            error_code = _sanitize_error_code(exc)
            logger.error("Synchronization failed for account %s: %s", account_id, error_code)
            status = SyncStatus.PARTIAL if pages_count > 0 else SyncStatus.FAILED

            await self._sync_run_repo.update_sync_run(
                sync_run_id,
                status=status,
                page_count=pages_count,
                message_count=messages_count,
                failed_message_count=failed_count,
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
                failed_message_count=failed_count,
                error_code=error_code,
            )
