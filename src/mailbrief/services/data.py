"""Offline owner exports and explicit, previewed retention. No provider or vault access."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.infra.files import write_text_atomically
from mailbrief.storage.database import Database
from mailbrief.storage.tables import (
    AccountTable,
    ActionTable,
    Base,
    DigestTable,
    DraftTable,
    DraftVersionTable,
    MessageTable,
    SuggestionDecisionTable,
    SyncRunTable,
)

EXPORT_TABLES = (
    "actions",
    "action_steps",
    "action_sources",
    "action_proposals",
    "drafts",
    "draft_versions",
    "draft_sources",
    "draft_generations",
)


class CleanupKind(StrEnum):
    CACHE = "cache"
    ACCOUNT = "account"
    DELETED = "deleted"
    ORPHAN_DECISIONS = "orphan-decisions"


@dataclass(frozen=True)
class CleanupRequest:
    kind: CleanupKind
    account_id: int | None = None
    before: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CleanupKind):
            raise ValueError("Unknown cleanup operation.")
        if self.before is not None and (
            self.before.tzinfo is None or self.before.utcoffset() is None
        ):
            raise ValueError("Cleanup needs an aware cutoff.")
        if self.kind in (CleanupKind.CACHE, CleanupKind.ACCOUNT):
            if self.account_id is None or self.account_id < 1:
                raise ValueError("Choose a saved account.")
        elif self.account_id is not None or self.before is None:
            raise ValueError("Deleted-writing cleanup needs a cutoff and no account.")
        if self.kind == CleanupKind.ACCOUNT and self.before is not None:
            raise ValueError("Account removal has no date cutoff.")


@dataclass(frozen=True)
class CleanupPreview:
    request: CleanupRequest
    fingerprint: str
    messages: int
    briefs: int
    sync_runs: int
    accounts: int
    actions: int
    drafts: int
    versions: int
    decisions: int

    @property
    def summary(self) -> str:
        return (
            f"Remove {self.messages} cached messages, {self.briefs} saved briefs, "
            f"{self.sync_runs} sync records, {self.accounts} accounts, "
            f"{self.actions} deleted actions, {self.drafts} deleted drafts/notes, "
            f"{self.versions} draft versions and {self.decisions} orphaned decisions."
        )


class CleanupChangedError(ValueError):
    def __init__(self) -> None:
        super().__init__("Saved data changed. Review a new cleanup preview.")


def _json(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError("Unsupported export value.")


async def _records(session: AsyncSession, names: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in names:
        table = Base.metadata.tables[name]
        rows = await session.execute(select(table).order_by(*table.primary_key.columns))
        result[name] = [dict(row) for row in rows.mappings()]
    return result


async def export_writing(database: Database, destination: Path) -> None:
    """Export owner writing and its history, preserving IDs and relationships."""
    async with database.session() as session:
        await session.execute(text("BEGIN"))
        records = await _records(session, EXPORT_TABLES)
    payload = (
        await asyncio.to_thread(
            json.dumps,
            {
                "format": "mailbrief-writing",
                "version": 1,
                "created_at_utc": datetime.now(UTC).isoformat(),
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
            default=_json,
        )
        + "\n"
    )
    await asyncio.to_thread(write_text_atomically, destination, payload, overwrite=False)


async def _selections(
    session: AsyncSession, request: CleanupRequest
) -> dict[type[Base], Select[Any]]:
    if request.kind == CleanupKind.ORPHAN_DECISIONS:
        account_exists = (
            select(AccountTable.id)
            .where(
                AccountTable.provider == SuggestionDecisionTable.provider,
                AccountTable.provider_account_id == SuggestionDecisionTable.provider_account_id,
            )
            .exists()
        )
        return {
            SuggestionDecisionTable: select(SuggestionDecisionTable).where(
                SuggestionDecisionTable.provider == "gmail",
                SuggestionDecisionTable.action_id.is_(None),
                SuggestionDecisionTable.decided_at_utc < request.before,
                ~account_exists,
            )
        }
    if request.kind == CleanupKind.DELETED:
        return {
            cls: select(cls).where(cls.deleted_at_utc < request.before)
            for cls in (ActionTable, DraftTable)
        }
    account = await session.get(AccountTable, request.account_id)
    if account is None or account.provider != "gmail":
        raise ValueError("Choose a saved Gmail account.")
    if request.kind == CleanupKind.ACCOUNT:
        return {AccountTable: select(AccountTable).where(AccountTable.id == account.id)}
    selections: dict[type[Base], Select[Any]] = {}
    for cls, timestamp in (
        (MessageTable, MessageTable.received_at_utc),
        (DigestTable, DigestTable.generated_at_utc),
        (SyncRunTable, SyncRunTable.started_at_utc),
    ):
        statement = select(cls).where(cls.account_id == account.id)
        if request.before is not None:
            statement = statement.where(timestamp < request.before)
        selections[cls] = statement
    return selections


async def _fingerprint(session: AsyncSession) -> str:
    digest = hashlib.sha256()
    for name, table in sorted(Base.metadata.tables.items()):
        digest.update(name.encode())
        rows = await session.stream(select(table).order_by(*table.primary_key.columns))
        try:
            async for chunk in rows.mappings().partitions(100):
                encoded = await asyncio.to_thread(
                    json.dumps,
                    [dict(row) for row in chunk],
                    sort_keys=True,
                    default=_json,
                    separators=(",", ":"),
                )
                digest.update(encoded.encode())
        finally:
            await rows.close()
    return digest.hexdigest()


async def _preview(session: AsyncSession, request: CleanupRequest) -> CleanupPreview:
    selections = await _selections(session, request)
    counts = {
        cls: await session.scalar(select(func.count()).select_from(query.subquery())) or 0
        for cls, query in selections.items()
    }
    if request.kind == CleanupKind.ACCOUNT:
        for cls in (MessageTable, DigestTable, SyncRunTable):
            counts[cls] = (
                await session.scalar(
                    select(func.count())
                    .select_from(cls)
                    .where(cls.account_id == request.account_id)
                )
                or 0
            )
    versions = 0
    if request.kind == CleanupKind.DELETED:
        versions = (
            await session.scalar(
                select(func.count())
                .select_from(DraftVersionTable)
                .join(DraftTable)
                .where(DraftTable.deleted_at_utc < request.before)
            )
            or 0
        )
    return CleanupPreview(
        request,
        await _fingerprint(session),
        counts.get(MessageTable, 0),
        counts.get(DigestTable, 0),
        counts.get(SyncRunTable, 0),
        counts.get(AccountTable, 0),
        counts.get(ActionTable, 0),
        counts.get(DraftTable, 0),
        versions,
        counts.get(SuggestionDecisionTable, 0),
    )


async def preview_cleanup(database: Database, request: CleanupRequest) -> CleanupPreview:
    async with database.session() as session:
        await session.execute(text("BEGIN"))
        return await _preview(session, request)


async def apply_cleanup(database: Database, preview: CleanupPreview) -> None:
    async with database.session() as session:
        # Recheck and delete under one write lock.
        await session.execute(text("BEGIN IMMEDIATE"))
        current = await _preview(session, preview.request)
        if current != preview:
            raise CleanupChangedError()
        for query in (await _selections(session, preview.request)).values():
            while rows := list(await session.scalars(query.limit(100))):
                for row in rows:
                    await session.delete(row)
                await session.flush()
        if preview.request.kind == CleanupKind.CACHE:
            account = await session.get(AccountTable, preview.request.account_id)
            if account is not None:
                account.last_sync_at_utc = None
        await session.commit()
