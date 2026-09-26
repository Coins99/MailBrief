"""Gmail connection checks, daily sync, body review and the consented AI daily brief."""

import argparse
import asyncio
import getpass
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from alembic.util import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from mailbrief.config import Settings
from mailbrief.domain.analysis import DeadlinePrecision
from mailbrief.domain.bodies import BodyStatus, PreparedBody
from mailbrief.domain.briefs import BriefRunResult, BriefStatus, TransmissionPreview
from mailbrief.domain.digests import DailyDigest, DigestItem, DigestStatus, SyncStatus
from mailbrief.errors import ConfigurationError
from mailbrief.paths import AppPaths
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderError
from mailbrief.providers.gmail.cache import GmailCredentialStore
from mailbrief.providers.gmail.errors import GmailSetupError
from mailbrief.providers.gmail.factory import gmail_auth, gmail_provider
from mailbrief.providers.openai.credentials import OpenAIKeyStore, parse_api_key
from mailbrief.providers.openai.factory import openai_provider
from mailbrief.services.analysis import AnalysisService
from mailbrief.services.application import ApplicationService
from mailbrief.services.bodies import BodyService
from mailbrief.services.brief import CONSENT_DISCLOSURE_VERSION, BriefService, disclosure_lines
from mailbrief.services.calendar import local_day_window, resolve_timezone
from mailbrief.services.digest import DigestService
from mailbrief.storage.database import Database
from mailbrief.storage.migrate import upgrade_database
from mailbrief.storage.repositories import (
    AccountRepository,
    ConsentRepository,
    MessageRepository,
    SyncRunRepository,
)

OPENAI = "openai"
_AI_COMMANDS = frozenset({"brief", "ai-key", "ai-consent"})
_SETUP_UNAVAILABLE = (
    "Gmail configuration or secure storage is unavailable. See docs/gmail-setup.md."
)
_AI_ERROR_MESSAGES = {
    "AI_AUTH_FAILED": "OpenAI rejected the API key. Run: mailbrief-gmail-diagnostic ai-key set",
    "AI_PERMISSION_DENIED": "OpenAI denied access (permission, region or quota).",
    "AI_RATE_LIMITED": "OpenAI rate limit reached; retry later.",
    "AI_TIMEOUT": "OpenAI did not respond in time.",
    "AI_PROVIDER_ERROR": "OpenAI request failed; check MAILBRIEF_OPENAI_MODEL.",
    "ANALYSIS_FAILED": "No message could be analyzed.",
}


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


def ai_key(action: str) -> int:
    """Save, check or remove the OpenAI API key; no part of the key is ever printed."""
    store = OpenAIKeyStore()
    if action == "set":
        try:
            raw = getpass.getpass("OpenAI API key (input hidden): ")
        except EOFError:
            raw = ""
        store.save(parse_api_key(raw))
        print("OpenAI API key saved in the OS credential store.")
    elif action == "status":
        print("OpenAI API key: saved" if store.load() is not None else "OpenAI API key: not saved")
    else:
        store.clear()
        print("OpenAI API key removed from the OS credential store.")
    return 0


async def ai_consent(action: str, *, database_path: Path | None) -> int:
    """Show or revoke recorded OpenAI consent for every account; needs no Gmail connection."""
    path = database_path or AppPaths.from_qt().database_path
    await asyncio.to_thread(upgrade_database, path)
    database = Database.from_path(path)
    try:
        async with database.session() as session:
            accounts = await AccountRepository(session).list_all()
            consents = ConsentRepository(session)
            if action == "status":
                if not accounts:
                    print("No accounts in this database.")
                for account in accounts:
                    active = await consents.get_active(
                        account.id, OPENAI, CONSENT_DISCLOSURE_VERSION
                    )
                    state = (
                        f"granted {active.granted_at_utc:%Y-%m-%d %H:%M} UTC"
                        if active is not None
                        else "not granted"
                    )
                    print(f"Account {account.id}: OpenAI consent {state}")
                return 0
            now = datetime.now(UTC)
            revoked = 0
            for account in accounts:
                revoked += await consents.revoke_all(account.id, OPENAI, now)
            await session.commit()
            print(f"OpenAI consent revoked: {revoked}")
            return 0
    finally:
        await database.dispose()


async def _ask(prompt: str) -> str:
    try:
        answer = await asyncio.to_thread(input, prompt)
    except EOFError:
        return ""
    return answer.strip()


class CliConsentGate:
    """Terminal consent: first use needs a typed "yes"; --yes covers only recorded consent."""

    def __init__(self, *, assume_yes: bool) -> None:
        self._assume_yes = assume_yes

    async def confirm(self, preview: TransmissionPreview) -> bool:
        for line in disclosure_lines(preview):
            print(line)
        if preview.first_use:
            if self._assume_yes:
                print("First use needs your interactive consent; run brief without --yes.")
                return False
            return await _ask('Type "yes" to consent and send: ') == "yes"
        if self._assume_yes:
            return True
        return (await _ask("Send? [y/N] ")).lower() in {"y", "yes"}


def _count(value: int | None) -> str:
    return "?" if value is None else str(value)


def _outcome(result: BriefRunResult) -> str:
    if result.status is BriefStatus.SAVED:
        return "Saved. Bodies were not stored."
    if result.status is BriefStatus.CONSENT_DECLINED:
        return "Nothing was sent. No brief saved."
    if result.status is BriefStatus.ANALYSIS_FAILED:
        message = _AI_ERROR_MESSAGES.get(
            result.error_code or "", _AI_ERROR_MESSAGES["ANALYSIS_FAILED"]
        )
        return f"{message} Your last saved brief for today is unchanged."
    if result.status is BriefStatus.SYNC_FAILED:
        return f"Sync failed ({result.error_code or 'unknown'}). Nothing was sent."
    return "Cancelled. No brief saved."


def _print_result(result: BriefRunResult, *, model: str) -> None:
    """Counts only: never subjects, senders or text."""
    digest = result.digest
    status = result.status.value + (f" ({digest.status.value})" if digest is not None else "")
    print(f"Brief: {status}; items: {len(digest.items) if digest is not None else 0}")
    coverage = result.coverage
    if coverage is not None:
        print(
            f"Coverage: shortlisted {coverage.shortlisted}, analyzed {coverage.analyzed}, "
            f"reused {coverage.reused}, failed {coverage.failed}, skipped {coverage.skipped}; "
            f"sync complete: {'yes' if coverage.sync_complete else 'no'}"
        )
        if coverage.analyzed == 0 and coverage.input_tokens is None:
            print("AI: nothing sent this run")
        else:
            print(
                f"AI: {OPENAI} / {coverage.ai_model or model}; tokens in/out: "
                f"{_count(coverage.input_tokens)} / {_count(coverage.output_tokens)}"
            )
    print(_outcome(result))


def _deadline(item: DigestItem, zone: ZoneInfo) -> str | None:
    if item.deadline_precision is DeadlinePrecision.DATETIME and item.deadline_at_utc is not None:
        return f"{item.deadline_at_utc.astimezone(zone):%Y-%m-%d %H:%M} ({zone.key})"
    if item.deadline_precision is DeadlinePrecision.DATE and item.deadline_date is not None:
        return item.deadline_date.isoformat()
    if item.deadline_precision is DeadlinePrecision.UNRESOLVED and item.deadline_text:
        return f'"{item.deadline_text}"'
    return None


def _print_items(digest: DailyDigest) -> None:
    """Derived brief content, shown only on request; never evidence or bodies."""
    zone = ZoneInfo(digest.timezone_name)
    for item in digest.items:
        print(f"{item.position + 1}. [{item.section.value}] {item.sender.address}: {item.subject}")
        print(f"   {item.summary}")
        if item.action_text:
            print(f"   Action: {item.action_text}")
        deadline = _deadline(item, zone)
        if deadline is not None:
            print(f"   Deadline: {deadline}")
        print(f"   {item.source_url}")


def _brief_exit_code(result: BriefRunResult) -> int:
    if result.status is BriefStatus.SAVED:
        complete = (DigestStatus.COMPLETE, DigestStatus.EMPTY)
        return 0 if result.digest is not None and result.digest.status in complete else 4
    if result.status is BriefStatus.CONSENT_DECLINED:
        return 6
    if result.status is BriefStatus.CANCELLED:
        return 130
    return 4


async def brief(
    *,
    silent_only: bool,
    database_path: Path | None,
    timezone: str | None,
    include_ids: tuple[str, ...],
    exclude_ids: tuple[str, ...],
    assume_yes: bool,
    show: bool,
) -> int:
    """Sync, ask consent, analyze the shortlist with OpenAI and save today's brief."""
    settings = Settings()
    tz = resolve_timezone(timezone)
    now = datetime.now(UTC)
    window = local_day_window(now, tz)
    path = database_path or AppPaths.from_qt().database_path
    async with (
        gmail_provider(settings, silent_only=silent_only) as provider,
        openai_provider(settings) as ai,
    ):
        await provider.connect()
        await asyncio.to_thread(upgrade_database, path)
        database = Database.from_path(path)
        try:
            async with database.session() as session:
                service = BriefService(
                    session=session,
                    application=ApplicationService(
                        provider,
                        MessageRepository(session),
                        SyncRunRepository(session),
                        AccountRepository(session),
                    ),
                    bodies=BodyService(provider, limit=settings.ai_body_character_limit),
                    analysis=AnalysisService(session, ai, batch_size=settings.ai_batch_size),
                    digests=DigestService(session),
                    consent_gate=CliConsentGate(assume_yes=assume_yes),
                    clock=lambda: now,
                )
                result = await service.generate(
                    tz_key=tz.key, include_ids=include_ids, exclude_ids=exclude_ids
                )
        finally:
            await database.dispose()
        model = ai.model_name
    print(f"Inbox date: {window.local_date} ({window.timezone_name})")
    _print_result(result, model=model)
    if show and result.digest is not None:
        _print_items(result.digest)
    return _brief_exit_code(result)


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
    brief_parser = commands.add_parser(
        "brief",
        help="Sync today's Inbox, analyze the shortlist with OpenAI after consent, save the brief.",
    )
    _add_day_options(brief_parser)
    brief_parser.add_argument(
        "--yes",
        action="store_true",
        help="Send without asking when consent is already recorded; first use still asks.",
    )
    brief_parser.add_argument(
        "--show",
        action="store_true",
        help="Print the saved items: sender, subject, summary, action, deadline and link.",
    )
    key_parser = commands.add_parser(
        "ai-key", help="Save, check or remove the OpenAI API key in the OS credential store."
    )
    key_parser.add_argument(
        "action",
        choices=("set", "status", "clear"),
        help="set prompts without echoing; status never shows the key; clear is safe to repeat.",
    )
    consent_parser = commands.add_parser(
        "ai-consent", help="Show or revoke your recorded consent to send email to OpenAI."
    )
    consent_parser.add_argument(
        "action", choices=("status", "revoke"), help="Show or revoke consent for every account."
    )
    consent_parser.add_argument(
        "--database", type=Path, help="Optional SQLite path; defaults to app data."
    )
    args = parser.parse_args(arguments)
    # Wire/debug logging can expose authorization headers, loopback URLs and request bodies.
    for name in ("httpx", "httpcore", "openai"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        if args.command == "brief":
            return asyncio.run(
                brief(
                    silent_only=args.silent_only,
                    database_path=args.database,
                    timezone=args.timezone,
                    include_ids=tuple(args.include),
                    exclude_ids=tuple(args.exclude),
                    assume_yes=args.yes,
                    show=args.show,
                )
            )
        if args.command == "ai-key":
            return ai_key(args.action)
        if args.command == "ai-consent":
            return asyncio.run(ai_consent(args.action, database_path=args.database))
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
    except ConfigurationError as exc:
        # AI setup errors (model, key, vault) carry static, actionable messages.
        print(str(exc) if args.command in _AI_COMMANDS else _SETUP_UNAVAILABLE)
        return 3
    except ValidationError:
        print(_SETUP_UNAVAILABLE)
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
