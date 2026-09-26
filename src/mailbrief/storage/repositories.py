"""Database repositories implementing transactional persistence and domain mappings."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import cast

from pydantic import HttpUrl
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.domain.analysis import (
    SUMMARY_MAX_CHARS,
    AnalysisCategory,
    DeadlinePrecision,
    MessageAnalysis,
)
from mailbrief.domain.common import normalize_utc
from mailbrief.domain.digests import (
    DailyDigest,
    DigestCoverage,
    DigestItem,
    DigestSection,
    DigestStatus,
    SyncStatus,
)
from mailbrief.domain.messages import (
    AccountIdentity,
    EmailContact,
    MessageImportance,
    NormalizedMessage,
    ProviderKind,
    RankReason,
)
from mailbrief.storage.tables import (
    AccountTable,
    AIConsentTable,
    AnalysisTable,
    DigestItemTable,
    DigestTable,
    MessageTable,
    SyncRunTable,
)
from mailbrief.text.prepare import truncate_at_boundary

# Batch size limit for bulk SQLite inserts to safeguard parameter limits
MAX_SQLITE_BATCH_SIZE = 100
_NO_SUMMARY = "No summary available"


class AccountRepository:
    """Repository managing connected provider accounts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, identity: AccountIdentity) -> AccountTable:
        """Insert or update an account identity idempotently."""
        provider_val = (
            identity.provider.value
            if isinstance(identity.provider, ProviderKind)
            else str(identity.provider)
        )
        base_stmt = sqlite_insert(AccountTable).values(
            provider=provider_val,
            provider_account_id=identity.provider_account_id,
            email_address=identity.email_address,
            display_name=identity.display_name,
            tenant_id=identity.tenant_id,
            account_addresses=list(identity.account_addresses)
            if identity.account_addresses
            else [identity.email_address],  # noqa: E501
            created_at_utc=datetime.now(UTC),
        )
        stmt = base_stmt.on_conflict_do_update(
            index_elements=["provider", "provider_account_id"],
            set_={
                "email_address": base_stmt.excluded.email_address,
                "display_name": base_stmt.excluded.display_name,
                "tenant_id": base_stmt.excluded.tenant_id,
                "account_addresses": base_stmt.excluded.account_addresses,
            },
        ).returning(AccountTable)
        account = await self._session.scalar(stmt)
        assert account is not None
        return account

    @staticmethod
    def to_domain(account: AccountTable) -> AccountIdentity:
        """Convert an AccountTable ORM model into a domain AccountIdentity."""
        return AccountIdentity(
            provider=ProviderKind(account.provider),
            provider_account_id=account.provider_account_id,
            email_address=account.email_address,
            display_name=account.display_name,
            tenant_id=account.tenant_id,
            account_addresses=tuple(account.account_addresses or (account.email_address,)),
        )

    async def get_by_id(self, account_id: int) -> AccountTable | None:
        """Fetch an account by its primary key ID."""
        return await self._session.get(AccountTable, account_id)

    async def get_by_provider_identity(
        self,
        provider: str | ProviderKind,
        provider_account_id: str,
    ) -> AccountTable | None:
        """Fetch an account by its unique provider identity."""
        provider_val = provider.value if isinstance(provider, ProviderKind) else str(provider)
        stmt = select(AccountTable).where(
            AccountTable.provider == provider_val,
            AccountTable.provider_account_id == provider_account_id,
        )
        result = await self._session.scalars(stmt)
        return result.first()

    async def get_by_email(self, email_address: str) -> AccountTable | None:
        """Fetch an account by its email address."""
        stmt = select(AccountTable).where(AccountTable.email_address == email_address)
        result = await self._session.scalars(stmt)
        return result.first()

    async def list_all(self) -> list[AccountTable]:
        """Return all persisted accounts ordered by ID."""
        stmt = select(AccountTable).order_by(AccountTable.id.asc())
        result = await self._session.scalars(stmt)
        return list(result.all())

    async def count(self) -> int:
        """Return the total number of connected accounts."""
        count_val = await self._session.scalar(select(func.count()).select_from(AccountTable))
        return count_val or 0

    async def update_last_sync(self, account_id: int, last_sync_at_utc: datetime) -> None:
        """Update the last synchronization timestamp for an account."""
        stmt = (
            update(AccountTable)
            .where(AccountTable.id == account_id)
            .values(last_sync_at_utc=normalize_utc(last_sync_at_utc))
        )
        await self._session.execute(stmt)

    async def delete_by_id(self, account_id: int) -> bool:
        """Delete an account and its cascade records by ID."""
        account = await self._session.get(AccountTable, account_id)
        if account is None:
            return False
        await self._session.delete(account)
        return True


class MessageRepository:
    """Repository managing normalized message metadata and ranking scores."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_messages(
        self,
        account_id: int,
        messages: Sequence[NormalizedMessage],
    ) -> list[MessageTable]:
        """Idempotently insert or update normalized message metadata in bulk.

        Deduplicates within the provided batch to protect against SQLite single-statement
        re-modification constraints and chunks queries into safe parameter sizes.
        """
        if not messages:
            return []

        # Deduplicate within batch preserving the latest item
        deduped: dict[str, NormalizedMessage] = {}
        for m in messages:
            deduped[m.provider_message_id] = m
        unique_messages = list(deduped.values())

        saved_rows: list[MessageTable] = []
        for i in range(0, len(unique_messages), MAX_SQLITE_BATCH_SIZE):
            batch = unique_messages[i : i + MAX_SQLITE_BATCH_SIZE]
            rows = [
                {
                    "account_id": account_id,
                    "provider_message_id": m.provider_message_id,
                    "internet_message_id": m.internet_message_id,
                    "conversation_id": m.conversation_id,
                    "subject": m.subject,
                    "sender_name": m.sender.name,
                    "sender_address": m.sender.address,
                    "to_recipients_json": [
                        {"name": r.name, "address": r.address} for r in m.to_recipients
                    ],
                    "received_at_utc": m.received_at_utc,
                    "is_read": m.is_read,
                    "is_in_inbox": m.is_in_inbox,
                    "importance": (
                        m.importance.value
                        if isinstance(m.importance, MessageImportance)
                        else str(m.importance)
                    ),
                    "has_attachments": m.has_attachments,
                    "body_preview": m.body_preview,
                    "web_link": str(m.web_link),
                    "rank_reasons_json": [],
                    "synced_at_utc": datetime.now(UTC),
                }
                for m in batch
            ]

            base_stmt = sqlite_insert(MessageTable).values(rows)
            stmt = base_stmt.on_conflict_do_update(
                index_elements=["account_id", "provider_message_id"],
                set_={
                    "internet_message_id": base_stmt.excluded.internet_message_id,
                    "conversation_id": base_stmt.excluded.conversation_id,
                    "subject": base_stmt.excluded.subject,
                    "sender_name": base_stmt.excluded.sender_name,
                    "sender_address": base_stmt.excluded.sender_address,
                    "to_recipients_json": base_stmt.excluded.to_recipients_json,
                    "received_at_utc": base_stmt.excluded.received_at_utc,
                    "is_read": base_stmt.excluded.is_read,
                    "is_in_inbox": base_stmt.excluded.is_in_inbox,
                    "importance": base_stmt.excluded.importance,
                    "has_attachments": base_stmt.excluded.has_attachments,
                    "body_preview": base_stmt.excluded.body_preview,
                    "web_link": base_stmt.excluded.web_link,
                    "synced_at_utc": base_stmt.excluded.synced_at_utc,
                },
            ).returning(MessageTable)

            result = await self._session.scalars(stmt)
            saved_rows.extend(result.all())

        return saved_rows

    async def update_ranking(
        self,
        message_id: int,
        rank_score: int,
        rank_reasons: Sequence[str | RankReason],
    ) -> None:
        """Persist computed rank score and readable reasons for a message."""
        reasons_list = [r.value if isinstance(r, RankReason) else str(r) for r in rank_reasons]
        stmt = (
            update(MessageTable)
            .where(MessageTable.id == message_id)
            .values(rank_score=rank_score, rank_reasons_json=reasons_list)
        )
        await self._session.execute(stmt)

    async def update_rankings(
        self,
        rankings: Sequence[tuple[int, int, Sequence[str | RankReason]]],
    ) -> None:
        """Persist computed rank scores and readable reasons for multiple messages by ID."""
        if not rankings:
            return
        for message_id, rank_score, rank_reasons in rankings:
            reasons_list = [r.value if isinstance(r, RankReason) else str(r) for r in rank_reasons]
            stmt = (
                update(MessageTable)
                .where(MessageTable.id == message_id)
                .values(rank_score=rank_score, rank_reasons_json=reasons_list)
            )
            await self._session.execute(stmt)

    async def update_rankings_by_provider_id(
        self,
        account_id: int,
        rankings: Sequence[tuple[str, int, Sequence[str | RankReason]]],
    ) -> None:
        """Persist rank scores and reasons for multiple messages by provider_message_id."""
        if not rankings:
            return
        for provider_msg_id, rank_score, rank_reasons in rankings:
            reasons_list = [r.value if isinstance(r, RankReason) else str(r) for r in rank_reasons]
            stmt = (
                update(MessageTable)
                .where(
                    MessageTable.account_id == account_id,
                    MessageTable.provider_message_id == provider_msg_id,
                )
                .values(rank_score=rank_score, rank_reasons_json=reasons_list)
            )
            await self._session.execute(stmt)

    async def get_messages_in_range(
        self,
        account_id: int,
        start_utc: datetime,
        end_utc: datetime,
        *,
        inbox_only: bool = False,
    ) -> list[MessageTable]:
        """Fetch all messages for an account within a UTC datetime range."""
        stmt = (
            select(MessageTable)
            .where(
                MessageTable.account_id == account_id,
                MessageTable.received_at_utc >= normalize_utc(start_utc),
                MessageTable.received_at_utc < normalize_utc(end_utc),
            )
            .order_by(MessageTable.received_at_utc.desc(), MessageTable.id.asc())
        )
        if inbox_only:
            stmt = stmt.where(MessageTable.is_in_inbox.is_(True))
        result = await self._session.scalars(stmt.execution_options(populate_existing=True))
        return list(result.all())

    async def reconcile_inbox(
        self, account_id: int, start_utc: datetime, end_utc: datetime, seen_ids: set[str]
    ) -> None:
        """Call only after complete enumeration; preserve cached content and references."""
        window = (
            MessageTable.account_id == account_id,
            MessageTable.received_at_utc >= normalize_utc(start_utc),
            MessageTable.received_at_utc < normalize_utc(end_utc),
        )
        await self._session.execute(update(MessageTable).where(*window).values(is_in_inbox=False))
        identifiers = sorted(seen_ids)
        for offset in range(0, len(identifiers), MAX_SQLITE_BATCH_SIZE):
            await self._session.execute(
                update(MessageTable)
                .where(
                    *window,
                    MessageTable.provider_message_id.in_(
                        identifiers[offset : offset + MAX_SQLITE_BATCH_SIZE]
                    ),
                )
                .values(is_in_inbox=True)
            )

    async def get_by_provider_message_id(
        self,
        account_id: int,
        provider_message_id: str,
    ) -> MessageTable | None:
        """Fetch a single message by its provider message ID."""
        stmt = select(MessageTable).where(
            MessageTable.account_id == account_id,
            MessageTable.provider_message_id == provider_message_id,
        )
        result = await self._session.scalars(stmt)
        return result.first()

    async def get_by_id(self, message_id: int) -> MessageTable | None:
        """Fetch a single message by its internal primary key ID."""
        return await self._session.get(MessageTable, message_id)

    @staticmethod
    def to_domain(
        row: MessageTable,
        provider_account_id: str,
        provider: ProviderKind,
    ) -> NormalizedMessage:
        """Map a MessageTable ORM entity to a NormalizedMessage domain model."""
        return NormalizedMessage(
            provider=provider,
            provider_account_id=provider_account_id,
            provider_message_id=row.provider_message_id,
            internet_message_id=row.internet_message_id,
            conversation_id=row.conversation_id,
            subject=row.subject,
            sender=EmailContact(name=row.sender_name, address=row.sender_address),
            to_recipients=tuple(
                EmailContact(name=r.get("name"), address=str(r.get("address", "")))
                for r in row.to_recipients_json
            ),
            received_at_utc=row.received_at_utc,
            is_read=row.is_read,
            is_in_inbox=row.is_in_inbox,
            importance=MessageImportance(row.importance),
            has_attachments=row.has_attachments,
            body_preview=row.body_preview,
            web_link=HttpUrl(row.web_link),
        )


class AnalysisRepository:
    """Repository managing structured AI analysis results and caching."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_analysis(
        self,
        message_id: int,
        input_hash: str,
        provider: str,
        model: str,
        prompt_version: str,
        schema_version: str,
        analysis: MessageAnalysis,
    ) -> AnalysisTable:
        """Idempotently insert or update a structured analysis result."""
        category_val = (
            analysis.category.value
            if isinstance(analysis.category, AnalysisCategory)
            else str(analysis.category)
        )
        base_stmt = sqlite_insert(AnalysisTable).values(
            message_id=message_id,
            input_hash=input_hash,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            schema_version=schema_version,
            category=category_val,
            summary=analysis.summary,
            action_required=analysis.action_required,
            action_text=analysis.action_text,
            deadline_text=analysis.deadline_text,
            deadline_at_utc=analysis.deadline_at_utc,
            deadline_precision=analysis.deadline_precision.value,
            deadline_date=analysis.deadline_date,
            deadline_timezone=analysis.deadline_timezone,
            confidence=analysis.confidence,
            evidence=analysis.evidence,
            analyzed_at_utc=datetime.now(UTC),
        )
        stmt = base_stmt.on_conflict_do_update(
            index_elements=[
                "message_id",
                "input_hash",
                "provider",
                "model",
                "prompt_version",
                "schema_version",
            ],
            set_={
                "category": base_stmt.excluded.category,
                "summary": base_stmt.excluded.summary,
                "action_required": base_stmt.excluded.action_required,
                "action_text": base_stmt.excluded.action_text,
                "deadline_text": base_stmt.excluded.deadline_text,
                "deadline_at_utc": base_stmt.excluded.deadline_at_utc,
                "deadline_precision": base_stmt.excluded.deadline_precision,
                "deadline_date": base_stmt.excluded.deadline_date,
                "deadline_timezone": base_stmt.excluded.deadline_timezone,
                "confidence": base_stmt.excluded.confidence,
                "evidence": base_stmt.excluded.evidence,
                "analyzed_at_utc": base_stmt.excluded.analyzed_at_utc,
            },
        ).returning(AnalysisTable)
        result_table = await self._session.scalar(stmt)
        assert result_table is not None
        return result_table

    async def get_cached_analysis(
        self,
        message_id: int,
        input_hash: str,
        provider: str,
        model: str,
        prompt_version: str,
        schema_version: str,
    ) -> AnalysisTable | None:
        """Retrieve the cached analysis matching all six cache-identity columns."""
        stmt = select(AnalysisTable).where(
            AnalysisTable.message_id == message_id,
            AnalysisTable.input_hash == input_hash,
            AnalysisTable.provider == provider,
            AnalysisTable.model == model,
            AnalysisTable.prompt_version == prompt_version,
            AnalysisTable.schema_version == schema_version,
        )
        result = await self._session.scalars(stmt)
        return result.first()

    async def get_by_message_id(self, message_id: int) -> list[AnalysisTable]:
        """Retrieve all analysis records associated with a message."""
        stmt = (
            select(AnalysisTable)
            .where(AnalysisTable.message_id == message_id)
            .order_by(AnalysisTable.analyzed_at_utc.desc())
        )
        result = await self._session.scalars(stmt)
        return list(result.all())

    @staticmethod
    def to_domain(row: AnalysisTable, message_key: str) -> MessageAnalysis:
        """Map an AnalysisTable ORM entity to a MessageAnalysis domain model."""
        return MessageAnalysis(
            message_key=message_key,
            category=AnalysisCategory(row.category),
            summary=row.summary,
            action_required=row.action_required,
            action_text=row.action_text,
            deadline_text=row.deadline_text,
            deadline_precision=DeadlinePrecision(row.deadline_precision),
            deadline_date=row.deadline_date,
            deadline_at_utc=row.deadline_at_utc,
            deadline_timezone=row.deadline_timezone,
            confidence=row.confidence,
            evidence=row.evidence,
        )


_COVERAGE_COLUMNS = (
    "sync_complete",
    "shortlisted_count",
    "analyzed_count",
    "reused_count",
    "failed_count",
    "skipped_count",
    "input_tokens",
    "output_tokens",
    "ai_provider",
    "ai_model",
)


def _coverage_columns(coverage: DigestCoverage | None) -> dict[str, bool | int | str | None]:
    """Map brief coverage onto digest columns; no coverage leaves every column NULL."""
    if coverage is None:
        return dict.fromkeys(_COVERAGE_COLUMNS)
    return {
        "sync_complete": coverage.sync_complete,
        "shortlisted_count": coverage.shortlisted,
        "analyzed_count": coverage.analyzed,
        "reused_count": coverage.reused,
        "failed_count": coverage.failed,
        "skipped_count": coverage.skipped,
        "input_tokens": coverage.input_tokens,
        "output_tokens": coverage.output_tokens,
        "ai_provider": coverage.ai_provider,
        "ai_model": coverage.ai_model,
    }


def _fallback_summary(preview: str, subject: str) -> str:
    """Summarize an unanalyzed item from its preview, else its subject, within the item limit."""
    for text in (preview.strip(), subject.strip()):
        if text:
            # Previews (up to 255 characters in Outlook) can exceed the summary limit.
            return truncate_at_boundary(text, SUMMARY_MAX_CHARS)[0]
    return _NO_SUMMARY


def _coverage_from_row(digest: DigestTable) -> DigestCoverage | None:
    """Rebuild brief coverage; briefs saved before M4 have none."""
    if digest.shortlisted_count is None:
        return None
    return DigestCoverage(
        sync_complete=bool(digest.sync_complete),
        shortlisted=digest.shortlisted_count,
        analyzed=digest.analyzed_count or 0,
        reused=digest.reused_count or 0,
        failed=digest.failed_count or 0,
        skipped=digest.skipped_count or 0,
        input_tokens=digest.input_tokens,
        output_tokens=digest.output_tokens,
        ai_provider=digest.ai_provider,
        ai_model=digest.ai_model,
    )


class DigestRepository:
    """Repository managing generated daily digests and digest item membership."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_digest(
        self,
        account_id: int,
        local_date: date,
        timezone_name: str,
        status: str | DigestStatus,
        items: Sequence[tuple[int, int | None, int, str | DigestSection]] = (),
        coverage: DigestCoverage | None = None,
    ) -> DigestTable:
        """Create or update a daily digest, its coverage and its ordered items atomically."""
        status_val = status.value if isinstance(status, DigestStatus) else str(status)
        coverage_values = _coverage_columns(coverage)
        base_stmt = sqlite_insert(DigestTable).values(
            account_id=account_id,
            local_date=local_date,
            timezone_name=timezone_name,
            status=status_val,
            generated_at_utc=datetime.now(UTC),
            **coverage_values,
        )
        stmt = (
            base_stmt.on_conflict_do_update(
                index_elements=["account_id", "local_date"],
                set_={
                    "timezone_name": base_stmt.excluded.timezone_name,
                    "status": base_stmt.excluded.status,
                    "generated_at_utc": base_stmt.excluded.generated_at_utc,
                    **{name: base_stmt.excluded[name] for name in coverage_values},
                },
            )
            .returning(DigestTable)
            # Refresh a digest already loaded in this session instead of returning it stale.
            .execution_options(populate_existing=True)
        )
        digest = await self._session.scalar(stmt)
        assert digest is not None

        # Delete any previous items for this digest and insert the new item list
        await self._session.execute(
            delete(DigestItemTable).where(DigestItemTable.digest_id == digest.id)
        )

        if items:
            item_rows = [
                {
                    "digest_id": digest.id,
                    "message_id": msg_id,
                    "analysis_id": analysis_id,
                    "position": pos,
                    "section": (sec.value if isinstance(sec, DigestSection) else str(sec)),
                }
                for msg_id, analysis_id, pos, sec in items
            ]
            await self._session.execute(insert(DigestItemTable).values(item_rows))

        return digest

    async def get_by_account_and_date(
        self,
        account_id: int,
        local_date: date,
    ) -> DigestTable | None:
        """Fetch a digest by its unique account and local date combination."""
        stmt = select(DigestTable).where(
            DigestTable.account_id == account_id,
            DigestTable.local_date == local_date,
        )
        result = await self._session.scalars(stmt)
        return result.first()

    async def get_digest_items(
        self,
        digest_id: int,
    ) -> list[tuple[DigestItemTable, MessageTable, AnalysisTable | None]]:
        """Fetch all ordered items for a digest joined with message and analysis records."""
        stmt = (
            select(DigestItemTable, MessageTable, AnalysisTable)
            .join(MessageTable, DigestItemTable.message_id == MessageTable.id)
            .outerjoin(AnalysisTable, DigestItemTable.analysis_id == AnalysisTable.id)
            .where(DigestItemTable.digest_id == digest_id)
            .order_by(DigestItemTable.position.asc())
        )
        result = await self._session.execute(stmt)
        rows = result.tuples().all()
        return cast(
            list[tuple[DigestItemTable, MessageTable, AnalysisTable | None]],
            rows,
        )

    async def get_by_id(self, digest_id: int) -> DigestTable | None:
        """Fetch a digest by its primary key ID."""
        return await self._session.get(DigestTable, digest_id)

    @staticmethod
    def to_domain(
        digest: DigestTable,
        items_with_relations: Sequence[tuple[DigestItemTable, MessageTable, AnalysisTable | None]],
        account_identity: str,
    ) -> DailyDigest:
        """Map a DigestTable and its joined item records to a DailyDigest domain model."""
        domain_items: list[DigestItem] = []
        for item_table, msg_table, analysis_table in items_with_relations:
            if analysis_table is None:
                summary_val = _fallback_summary(msg_table.body_preview, msg_table.subject)
            else:
                summary_val = analysis_table.summary or _NO_SUMMARY
            domain_items.append(
                DigestItem(
                    message_key=msg_table.provider_message_id,
                    section=DigestSection(item_table.section),
                    position=item_table.position,
                    subject=msg_table.subject,
                    sender=EmailContact(
                        name=msg_table.sender_name,
                        address=msg_table.sender_address,
                    ),
                    summary=summary_val,
                    action_text=analysis_table.action_text if analysis_table else None,
                    deadline_text=analysis_table.deadline_text if analysis_table else None,
                    deadline_precision=(
                        DeadlinePrecision(analysis_table.deadline_precision)
                        if analysis_table
                        else DeadlinePrecision.NONE
                    ),
                    deadline_date=analysis_table.deadline_date if analysis_table else None,
                    deadline_at_utc=analysis_table.deadline_at_utc if analysis_table else None,
                    evidence=analysis_table.evidence if analysis_table else None,
                    source_url=HttpUrl(msg_table.web_link),
                )
            )

        return DailyDigest(
            account_id=account_identity,
            local_date=digest.local_date,
            timezone_name=digest.timezone_name,
            generated_at_utc=digest.generated_at_utc,
            status=DigestStatus(digest.status),
            items=tuple(domain_items),
            coverage=_coverage_from_row(digest),
        )


class ConsentRepository:
    """Repository recording per-account consent to send minimized content to an AI provider."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(
        self,
        account_id: int,
        provider: str,
        disclosure_version: str,
    ) -> AIConsentTable | None:
        """Return the consent for this disclosure version unless it has been revoked."""
        stmt = select(AIConsentTable).where(
            AIConsentTable.account_id == account_id,
            AIConsentTable.provider == provider,
            AIConsentTable.disclosure_version == disclosure_version,
            AIConsentTable.revoked_at_utc.is_(None),
        )
        result = await self._session.scalars(stmt.execution_options(populate_existing=True))
        return result.first()

    async def grant(
        self,
        account_id: int,
        provider: str,
        disclosure_version: str,
        now_utc: datetime,
    ) -> AIConsentTable:
        """Record consent, reactivating a revoked grant of the same disclosure version."""
        base_stmt = sqlite_insert(AIConsentTable).values(
            account_id=account_id,
            provider=provider,
            disclosure_version=disclosure_version,
            granted_at_utc=normalize_utc(now_utc),
            revoked_at_utc=None,
        )
        stmt = (
            base_stmt.on_conflict_do_update(
                index_elements=["account_id", "provider", "disclosure_version"],
                set_={
                    "granted_at_utc": base_stmt.excluded.granted_at_utc,
                    "revoked_at_utc": None,
                },
            )
            .returning(AIConsentTable)
            .execution_options(populate_existing=True)
        )
        consent = await self._session.scalar(stmt)
        assert consent is not None
        return consent

    async def revoke_all(self, account_id: int, provider: str, now_utc: datetime) -> int:
        """Revoke every active consent for the provider and return how many were revoked."""
        stmt = (
            update(AIConsentTable)
            .where(
                AIConsentTable.account_id == account_id,
                AIConsentTable.provider == provider,
                AIConsentTable.revoked_at_utc.is_(None),
            )
            .values(revoked_at_utc=normalize_utc(now_utc))
            .returning(AIConsentTable.id)
        )
        revoked = await self._session.scalars(stmt)
        return len(revoked.all())


class SyncRunRepository:
    """Repository managing audit history for synchronization runs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_sync_run(
        self,
        account_id: int,
        range_start_utc: datetime,
        range_end_utc: datetime,
        status: str | SyncStatus = "started",
    ) -> SyncRunTable:
        """Record the initiation of a mailbox synchronization run."""
        status_val = status.value if isinstance(status, SyncStatus) else str(status)
        sync_run = SyncRunTable(
            account_id=account_id,
            range_start_utc=normalize_utc(range_start_utc),
            range_end_utc=normalize_utc(range_end_utc),
            started_at_utc=datetime.now(UTC),
            status=status_val,
            page_count=0,
            message_count=0,
        )
        self._session.add(sync_run)
        await self._session.flush()
        return sync_run

    async def update_sync_run(
        self,
        sync_run_id: int,
        *,
        status: str | SyncStatus | None = None,
        page_count: int | None = None,
        message_count: int | None = None,
        failed_message_count: int | None = None,
        error_code: str | None = None,
        completed: bool = False,
    ) -> SyncRunTable | None:
        """Update progress or terminal state of an existing sync run."""
        sync_run = await self._session.get(SyncRunTable, sync_run_id)
        if not sync_run:
            return None

        if status is not None:
            sync_run.status = status.value if isinstance(status, SyncStatus) else str(status)
        if page_count is not None:
            sync_run.page_count = page_count
        if message_count is not None:
            sync_run.message_count = message_count
        if failed_message_count is not None:
            sync_run.failed_message_count = failed_message_count
        if error_code is not None:
            sync_run.sanitized_error_code = error_code
        if completed:
            sync_run.completed_at_utc = datetime.now(UTC)

        await self._session.flush()
        return sync_run

    async def get_latest_sync_run(self, account_id: int) -> SyncRunTable | None:
        """Retrieve the most recent sync run record for an account."""
        stmt = (
            select(SyncRunTable)
            .where(SyncRunTable.account_id == account_id)
            .order_by(SyncRunTable.started_at_utc.desc())
            .limit(1)
        )
        result = await self._session.scalars(stmt)
        return result.first()

    async def get_by_id(self, sync_run_id: int) -> SyncRunTable | None:
        """Fetch a sync run record by its primary key ID."""
        return await self._session.get(SyncRunTable, sync_run_id)
