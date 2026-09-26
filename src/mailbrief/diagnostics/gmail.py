"""Gmail connection checks, daily metadata sync and in-memory review of shortlisted bodies."""

import argparse
import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from alembic.util import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from mailbrief.config import Settings
from mailbrief.domain.bodies import BodyStatus, PreparedBody
from mailbrief.domain.digests import SyncStatus
from mailbrief.errors import ConfigurationError
from mailbrief.paths import AppPaths
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderError
from mailbrief.providers.gmail.cache import GmailCredentialStore
from mailbrief.providers.gmail.errors import GmailSetupError
from mailbrief.providers.gmail.factory import gmail_auth, gmail_provider
from mailbrief.services.application import ApplicationService
from mailbrief.services.bodies import BodyService
from mailbrief.services.calendar import local_day_window, resolve_timezone
from mailbrief.storage.database import Database
from mailbrief.storage.migrate import upgrade_database
from mailbrief.storage.repositories import AccountRepository, MessageRepository, SyncRunRepository


async def run(command: str, *, silent_only: bool) -> int:
    """Verify a profile or forget credentials; output contains no mailbox identity."""
    if command == "disconnect":
        await asyncio.to_thread(GmailCredentialStore().clear)
        print("Local Gmail credentials removed. Google access and local app data are unchanged.")
        return 0

    async with gmail_auth(Settings()) as auth:
        await auth.connect(silent_only=silent_only)
        print("Gmail read-only connection verified. No messages downloaded (M1).")
        return 0


async def sync(
    *,
    silent_only: bool,
    database_path: Path | None,
    timezone: str | None,
    include_ids: tuple[str, ...],
    exclude_ids: tuple[str, ...],
    show_metadata: bool,
) -> int:
    """Persist daily metadata and report coverage without printing mail by default."""
    tz = resolve_timezone(timezone)
    now = datetime.now(UTC)
    window = local_day_window(now, tz)
    path = database_path or AppPaths.from_qt().database_path
    async with gmail_provider(Settings(), silent_only=silent_only) as provider:
        await provider.connect()
        await asyncio.to_thread(upgrade_database, path)
        database = Database.from_path(path)
        try:
            async with database.session() as session:
                accounts = AccountRepository(session)
                messages = MessageRepository(session)
                application = ApplicationService(
                    provider, messages, SyncRunRepository(session), accounts
                )
                result, shortlist = await application.prepare_daily_shortlist(
                    tz_key=tz.key, now_utc=now, include_ids=include_ids, exclude_ids=exclude_ids
                )
                identity = await provider.current_account()
                assert identity is not None
                account = await accounts.get_by_provider_identity(
                    identity.provider, identity.provider_account_id
                )
                assert account is not None
                # Refresh ORM state after the sync's committed update.
                await session.refresh(account)
                print(f"Inbox date: {window.local_date} ({window.timezone_name})")
                print(
                    f"Status: {result.status.value}; pages: {result.page_count}; "
                    f"retrieved: {result.message_count}; "
                    f"failed items: {result.failed_message_count}; "
                    f"selected: {len(shortlist)}"
                )
                print(f"Last complete sync (UTC): {account.last_sync_at_utc or 'never'}")
                print("Metadata only: no full bodies, attachments, AI calls, or mailbox changes.")
                if result.error_code:
                    print(
                        f"Incomplete coverage: {result.error_code}. Cached metadata may be older."
                    )
                if show_metadata:
                    selected = {item.message.provider_message_id for item in shortlist}
                    rows = await messages.get_messages_in_range(
                        account.id, window.start_utc, window.end_utc, inbox_only=True
                    )
                    for row in rows:
                        marker = "selected" if row.provider_message_id in selected else "omitted"
                        print(
                            f"{row.provider_message_id} [{marker}] score={row.rank_score} "
                            f"{row.sender_address}: {row.subject}"
                        )
                        print(f"  {row.web_link}")
                return 0 if result.status is SyncStatus.COMPLETE else 4
        finally:
            await database.dispose()


def _describe(index: int, body: PreparedBody) -> str:
    flags = [
        name
        for name, present in (
            ("quoted history removed", body.quoted_history_removed),
            ("truncated", body.truncated),
        )
        if present
    ]
    return (
        f"{index}. {body.status.value}; source: {body.source.value}; "
        f"characters: {body.original_chars} -> {len(body.text)}; "
        f"attachments skipped: {body.attachments_skipped}; "
        f"unreadable parts: {body.unreadable_parts}" + "".join(f"; {flag}" for flag in flags)
    )


async def bodies(
    *,
    silent_only: bool,
    database_path: Path | None,
    timezone: str | None,
    include_ids: tuple[str, ...],
    exclude_ids: tuple[str, ...],
    show_text: bool,
) -> int:
    """Sync today's metadata, then read and prepare the shortlist in memory only."""
    settings = Settings()
    tz = resolve_timezone(timezone)
    now = datetime.now(UTC)
    path = database_path or AppPaths.from_qt().database_path
    async with gmail_provider(settings, silent_only=silent_only) as provider:
        await provider.connect()
        await asyncio.to_thread(upgrade_database, path)
        database = Database.from_path(path)
        try:
            async with database.session() as session:
                application = ApplicationService(
                    provider,
                    MessageRepository(session),
                    SyncRunRepository(session),
                    AccountRepository(session),
                )
                result, shortlist = await application.prepare_daily_shortlist(
                    tz_key=tz.key, now_utc=now, include_ids=include_ids, exclude_ids=exclude_ids
                )
        finally:
            await database.dispose()
        prepared = await BodyService(provider, limit=settings.ai_body_character_limit).prepare(
            shortlist
        )
    print(f"Sync status: {result.status.value}; selected: {len(shortlist)}")
    for index, body in enumerate(prepared, start=1):
        print(_describe(index, body))
    print("Bodies were read into memory only: nothing was saved, logged or sent anywhere.")
    if show_text:
        print("\nPrepared text follows, for this terminal only. It is not saved or logged.")
        for index, (item, body) in enumerate(zip(shortlist, prepared, strict=True), start=1):
            print(f"\n===== {index}. {item.message.sender.address}: {item.message.subject or ''}")
            print(body.text or f"({body.status.value}: no text)")
    failed = any(body.status is BodyStatus.FAILED for body in prepared)
    return 0 if result.status is SyncStatus.COMPLETE and not failed else 4


def _add_day_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--silent-only", action="store_true", help="Never open a browser.")
    parser.add_argument("--database", type=Path, help="Optional SQLite path; defaults to app data.")
    parser.add_argument("--timezone", help="IANA timezone; defaults to the system timezone.")
    parser.add_argument(
        "--include", action="append", default=[], help="Include a message ID in this shortlist."
    )
    parser.add_argument(
        "--exclude", action="append", default=[], help="Exclude a message ID from this shortlist."
    )


def main(arguments: Sequence[str] | None = None) -> int:
    """Return predictable exit codes without dumping provider payloads or secrets."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Verify Gmail identity; M1 does not fetch mail.")
    fetch.add_argument("--silent-only", action="store_true", help="Never open a browser.")
    commands.add_parser("disconnect", help="Remove only locally saved Gmail credentials.")
    sync_parser = commands.add_parser(
        "sync", help="Sync today's Inbox metadata to SQLite and rank it."
    )
    _add_day_options(sync_parser)
    sync_parser.add_argument(
        "--show-metadata",
        action="store_true",
        help="Print sender, subject, IDs and links for review.",
    )
    bodies_parser = commands.add_parser(
        "bodies", help="Sync, then read and prepare the shortlist's bodies in memory only."
    )
    _add_day_options(bodies_parser)
    bodies_parser.add_argument(
        "--show-text",
        action="store_true",
        help="Print the prepared text in this terminal for review (never saved or logged).",
    )
    args = parser.parse_args(arguments)
    # Wire/debug logging can expose authorization headers and loopback URLs.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        if args.command == "sync":
            return asyncio.run(
                sync(
                    silent_only=args.silent_only,
                    database_path=args.database,
                    timezone=args.timezone,
                    include_ids=tuple(args.include),
                    exclude_ids=tuple(args.exclude),
                    show_metadata=args.show_metadata,
                )
            )
        if args.command == "bodies":
            return asyncio.run(
                bodies(
                    silent_only=args.silent_only,
                    database_path=args.database,
                    timezone=args.timezone,
                    include_ids=tuple(args.include),
                    exclude_ids=tuple(args.exclude),
                    show_text=args.show_text,
                )
            )
        return asyncio.run(run(args.command, silent_only=getattr(args, "silent_only", False)))
    except AuthenticationRequiredError as exc:
        print(str(exc))
        return 2
    except GmailSetupError as exc:
        print(str(exc))
        return 3
    except (ConfigurationError, ValidationError):
        print("Gmail configuration or secure storage is unavailable. See docs/gmail-setup.md.")
        return 3
    except ProviderError as exc:
        print(str(exc))
        return 4
    except ValueError:
        print(
            "Invalid timezone or shortlist choices; "
            "IDs must belong to today's Inbox without overlap."
        )
        return 3
    except (SQLAlchemyError, OSError, CommandError):
        print(
            "Local database or file operation failed. "
            "Check access and retry; no mailbox changes made."
        )
        return 5
    except KeyboardInterrupt:
        print("Gmail diagnostic cancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
