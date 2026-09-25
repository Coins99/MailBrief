"""Top-level application workflow coordinating synchronization, ranking, and shortlisting."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from mailbrief.domain.common import normalize_utc
from mailbrief.domain.digests import SyncProgress, SyncResult, SyncStage, SyncStatus
from mailbrief.domain.messages import ProviderKind, RankedMessage
from mailbrief.ports.email_provider import EmailProvider
from mailbrief.services.calendar import local_day_window, resolve_timezone
from mailbrief.services.ranking import rank_messages, review_shortlist
from mailbrief.services.sync import SyncService
from mailbrief.storage.repositories import (
    AccountRepository,
    MessageRepository,
    SyncRunRepository,
)
from mailbrief.storage.tables import AccountTable

logger = logging.getLogger(__name__)


class ApplicationService:
    """Orchestrates account restoration, mailbox synchronization, and local ranking."""

    def __init__(
        self,
        provider: EmailProvider,
        message_repo: MessageRepository,
        sync_run_repo: SyncRunRepository,
        account_repo: AccountRepository,
        sync_service: SyncService | None = None,
    ) -> None:
        self._provider = provider
        self._message_repo = message_repo
        self._sync_run_repo = sync_run_repo
        self._account_repo = account_repo
        self._sync_service = sync_service or SyncService(
            provider=provider,
            message_repo=message_repo,
            sync_run_repo=sync_run_repo,
            account_repo=account_repo,
        )
        self._session = getattr(message_repo, "_session", None)

    async def get_or_restore_account(self) -> AccountTable:
        """Connect or restore an existing account session and persist identity."""
        identity = await self._provider.connect()
        account = await self._account_repo.upsert(identity)
        if self._session:
            await self._session.commit()
        return account

    async def prepare_daily_shortlist(
        self,
        *,
        tz_key: str | None = None,
        now_utc: datetime | None = None,
        progress: Callable[[SyncProgress], None] | None = None,
        cancel: asyncio.Event | None = None,
        include_ids: tuple[str, ...] = (),
        exclude_ids: tuple[str, ...] = (),
    ) -> tuple[SyncResult, list[RankedMessage]]:
        """Run the Milestone 3 vertical slice: account -> window -> sync -> ranking -> shortlist.

        Returns the terminal SyncResult and deterministic shortlisted RankedMessage items.
        """
        now = normalize_utc(now_utc) if now_utc is not None else datetime.now(UTC)
        account = await self.get_or_restore_account()

        tz = resolve_timezone(tz_key)
        window = local_day_window(now, tz)

        # 1. Sync messages from provider
        sync_result = await self._sync_service.sync_day(
            account_id=account.id,
            account_identity=account.email_address,
            window=window,
            progress=progress,
            cancel=cancel,
        )

        if sync_result.status in {SyncStatus.CANCELLED, SyncStatus.FAILED}:
            return sync_result, []

        if cancel and cancel.is_set():
            return (
                SyncResult(
                    account_id=sync_result.account_id,
                    range_start_utc=sync_result.range_start_utc,
                    range_end_utc=sync_result.range_end_utc,
                    status=SyncStatus.CANCELLED,
                    page_count=sync_result.page_count,
                    message_count=sync_result.message_count,
                    failed_message_count=sync_result.failed_message_count,
                ),
                [],
            )

        # 2. Read back cached messages from database for the day window
        rows = await self._message_repo.get_messages_in_range(
            account.id,
            window.start_utc,
            window.end_utc,
            inbox_only=True,
        )

        if not rows:
            review_shortlist([], include_ids=include_ids, exclude_ids=exclude_ids)
            return sync_result, []

        provider_kind = ProviderKind(account.provider)

        domain_messages = [
            MessageRepository.to_domain(row, account.provider_account_id, provider_kind)
            for row in rows
        ]

        if progress:
            try:
                progress(
                    SyncProgress(
                        stage=SyncStage.RANKING,
                        pages_fetched=sync_result.page_count,
                        messages_fetched=sync_result.message_count,
                    )
                )
            except Exception as exc:
                logger.warning("Progress callback raised an exception during ranking: %s", exc)

        # 3. Deterministic local ranking
        user_addresses = (
            account.account_addresses
            if getattr(account, "account_addresses", None)
            else (account.email_address,)
        )  # noqa: E501
        ranked = rank_messages(domain_messages, user_email=user_addresses, now_utc=now)  # type: ignore[arg-type]

        # 4. Batch persist computed rankings in SQLite
        rankings_data = [(row.id, r.score, r.reasons) for row, r in zip(rows, ranked, strict=True)]
        await self._message_repo.update_rankings(rankings_data)
        if self._session:
            await self._session.commit()

        # 5. Deterministic shortlist selection
        shortlist = review_shortlist(ranked, include_ids=include_ids, exclude_ids=exclude_ids)
        shortlist_keys = tuple(m.message.provider_message_id for m in shortlist)

        final_sync_result = SyncResult(
            account_id=sync_result.account_id,
            range_start_utc=sync_result.range_start_utc,
            range_end_utc=sync_result.range_end_utc,
            status=sync_result.status,
            page_count=sync_result.page_count,
            message_count=sync_result.message_count,
            failed_message_count=sync_result.failed_message_count,
            shortlisted_message_keys=shortlist_keys,
            error_code=sync_result.error_code,
        )

        return final_sync_result, shortlist
