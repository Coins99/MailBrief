"""Gmail connection checks, daily sync, body review, the consented AI brief, actions and
local drafts."""

import argparse
import asyncio
import contextlib
import getpass
import logging
import threading
import unicodedata
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from alembic.util import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from mailbrief.config import Settings
from mailbrief.domain.actions import (
    TARGET_REASON_TEXT,
    ActionFilter,
    ActionStatus,
    SuggestionState,
)
from mailbrief.domain.analysis import ActionOwnership, DeadlinePrecision
from mailbrief.domain.bodies import BodyStatus, PreparedBody
from mailbrief.domain.briefs import BriefRunResult, BriefStatus, TransmissionPreview
from mailbrief.domain.digests import DailyDigest, DigestItem, DigestStatus, SyncStatus
from mailbrief.domain.drafts import KIND_NAMES, export_markdown, export_text, still_to_fill
from mailbrief.errors import ConfigurationError
from mailbrief.infra.files import write_text_atomically
from mailbrief.paths import AppPaths
from mailbrief.ports.errors import AuthenticationRequiredError, ProviderError
from mailbrief.providers.gmail.cache import GmailCredentialStore
from mailbrief.providers.gmail.errors import GmailSetupError
from mailbrief.providers.gmail.factory import gmail_auth, gmail_provider
from mailbrief.providers.groq.credentials import GroqKeyStore, parse_api_key
from mailbrief.providers.groq.factory import groq_provider
from mailbrief.providers.groq.provider import KEY_MISSING_MESSAGE, PROVIDER_NAME
from mailbrief.services.actions import (
    COMPLETED_LIST_LIMIT,
    ActionConflictError,
    ActionNotFoundError,
    ActionService,
    SuggestionNotFoundError,
)
from mailbrief.services.analysis import AnalysisService
from mailbrief.services.application import ApplicationService
from mailbrief.services.bodies import BodyService
from mailbrief.services.brief import (
    CONSENT_DISCLOSURE_VERSION,
    BriefService,
    disclosure_lines,
    provider_display_name,
)
from mailbrief.services.calendar import InvalidTimezoneError, local_day_window, resolve_timezone
from mailbrief.services.digest import DigestService
from mailbrief.services.drafts import DraftNotFoundError, DraftService
from mailbrief.services.ranking import ShortlistReviewError
from mailbrief.storage.database import Database
from mailbrief.storage.migrate import upgrade_database
from mailbrief.storage.repositories import (
    AccountRepository,
    ConsentRepository,
    MessageRepository,
    SyncRunRepository,
)
from mailbrief.text.prepare import clean_generated_text

_AI_COMMANDS = frozenset({"brief", "ai-key", "ai-consent"})
# Commands whose setup errors are shown as they are: static, actionable messages.
_OWN_MESSAGE_COMMANDS = _AI_COMMANDS | {"actions", "drafts"}
_ACTION_REFUSED = "That suggestion or action was not found or cannot change now."
_DRAFT_NOT_FOUND = "That draft was not found."
_FILE_EXISTS = "That file already exists; nothing was written. Choose another --out path."
_SETUP_UNAVAILABLE = (
    "Gmail configuration or secure storage is unavailable. See docs/gmail-setup.md."
)
_AI_ERROR_MESSAGES = {
    "AI_USAGE_LIMIT": (
        "MailBrief's AI request limit for this run was reached. "
        "Review MAILBRIEF_AI_MAX_REQUESTS_PER_RUN before running again."
    ),
    "AI_KEY_MISSING": KEY_MISSING_MESSAGE,
    "AI_AUTH_FAILED": "Groq rejected the API key. Run: mailbrief-gmail-diagnostic ai-key set",
    "AI_NETWORK_BLOCKED": (
        "Groq refused this network. Groq blocks many VPN, proxy and data-centre connections; "
        "try your usual home or mobile connection."
    ),
    "AI_PERMISSION_DENIED": "Groq denied access (network, region, permission or quota).",
    "AI_RATE_LIMITED": "Groq rate limit reached; retry later.",
    "AI_TIMEOUT": "Groq did not respond in time.",
    "AI_SERVER_ERROR": "Groq had a server problem or sent an unreadable reply; retry later.",
    "AI_REQUEST_REJECTED": (
        "Groq rejected some messages, so they were left out. If every message is rejected, "
        "check that MAILBRIEF_GROQ_MODEL supports Structured Outputs."
    ),
    "AI_NETWORK_ERROR": "Could not reach Groq; check your connection and retry.",
    "AI_PROVIDER_ERROR": "Groq request failed; check MAILBRIEF_GROQ_MODEL.",
    "ANALYSIS_FAILED": "No message could be analyzed.",
}


class SettingsError(ConfigurationError):
    """A MailBrief setting is invalid. Messages name the settings, never their values."""


def _load_settings() -> Settings:
    """Build Settings, naming each invalid MAILBRIEF_* variable without its value."""
    try:
        return Settings()
    except ValidationError as exc:
        names = sorted(
            {f"MAILBRIEF_{str(error['loc'][0]).upper()}" for error in exc.errors() if error["loc"]}
        )
        listed = ", ".join(names) or "MAILBRIEF_* settings"
        raise SettingsError(f"Invalid setting: {listed}. See docs/ai-analysis.md.") from None


async def run(command: str, *, silent_only: bool) -> int:
    """Verify a profile or forget credentials; output contains no mailbox identity."""
    if command == "disconnect":
        await asyncio.to_thread(GmailCredentialStore().clear)
        print("Local Gmail credentials removed. Google access and local app data are unchanged.")
        return 0

    async with gmail_auth(_load_settings()) as auth:
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
    async with gmail_provider(_load_settings(), silent_only=silent_only) as provider:
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
    settings = _load_settings()
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
    """Save, check or remove the Groq API key; no part of the key is ever printed."""
    store = GroqKeyStore()
    if action == "set":
        try:
            raw = getpass.getpass("Groq API key (input hidden): ")
        except EOFError:
            raw = ""
        store.save(parse_api_key(raw))
        print("Groq API key saved in the OS credential store.")
    elif action == "status":
        print("Groq API key: saved" if store.load() is not None else "Groq API key: not saved")
    else:
        store.clear()
        print("Groq API key removed from the OS credential store.")
    return 0


async def ai_consent(action: str, *, database_path: Path | None) -> int:
    """Show or revoke recorded Groq consent for every account; needs no Gmail connection."""
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
                        account.id, PROVIDER_NAME, CONSENT_DISCLOSURE_VERSION
                    )
                    state = (
                        f"granted {active.granted_at_utc:%Y-%m-%d %H:%M} UTC"
                        if active is not None
                        else "not granted"
                    )
                    print(f"Account {account.id}: Groq consent {state}")
                return 0
            now = datetime.now(UTC)
            revoked = 0
            for account in accounts:
                revoked += await consents.revoke_all(account.id, PROVIDER_NAME, now)
            await session.commit()
            print(f"Groq consent revoked: {revoked}")
            return 0
    finally:
        await database.dispose()


async def _ask(prompt: str) -> str:
    """Read one answer in a daemon thread, so Ctrl+C never waits for Enter; EOF answers ""."""
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[str] = loop.create_future()

    def settle(result: str | Exception) -> None:
        if answer.done():
            return
        if isinstance(result, Exception):
            answer.set_exception(result)
        else:
            answer.set_result(result)

    def read() -> None:
        result: str | Exception
        try:
            result = input(prompt)
        except EOFError:
            result = ""
        except Exception as exc:
            result = exc
        # After Ctrl+C the loop may be closed; nobody is waiting for this answer then.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(settle, result)

    threading.Thread(target=read, name="mailbrief-prompt", daemon=True).start()
    return (await answer).strip()


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
        return _AI_ERROR_MESSAGES.get(
            result.error_code or "", _AI_ERROR_MESSAGES["ANALYSIS_FAILED"]
        )
    if result.status is BriefStatus.SYNC_FAILED:
        return f"Sync failed ({result.error_code or 'unknown'}). Nothing was sent."
    return "Cancelled. No brief saved."


def _ai_line(result: BriefRunResult, *, model: str) -> str:
    """Say nothing was sent only when no provider request was made, even a failed one."""
    if result.ai_calls == 0:
        return "AI: nothing sent this run"
    coverage = result.coverage
    tokens_in = coverage.input_tokens if coverage is not None else None
    tokens_out = coverage.output_tokens if coverage is not None else None
    used_model = (coverage.ai_model if coverage is not None else None) or model
    return (
        f"AI: {provider_display_name(PROVIDER_NAME)} / {used_model}; "
        f"requests: {result.ai_calls}; "
        f"tokens in/out: {_count(tokens_in)} / {_count(tokens_out)}"
    )


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
    if coverage is not None or result.ai_calls > 0:
        print(_ai_line(result, model=model))
    print(_outcome(result))
    # A partial brief still explains why the new messages failed (e.g. a wrong API key).
    partial_reason = _AI_ERROR_MESSAGES.get(result.error_code or "")
    if result.status is BriefStatus.SAVED and partial_reason is not None:
        print(partial_reason)
    if result.provider_detail:
        print(f"{provider_display_name(PROVIDER_NAME)} detail: {result.provider_detail}")
    if result.status is BriefStatus.ANALYSIS_FAILED:
        # Its own line: some messages end in a command, which must not run into this note.
        print("Your last saved brief for today is unchanged.")
    if _sync_incomplete(result):
        print("Inbox sync was incomplete, so this brief may be missing messages.")


def _sync_incomplete(result: BriefRunResult) -> bool:
    return result.coverage is not None and not result.coverage.sync_complete


def _format_deadline(
    precision: DeadlinePrecision,
    deadline_date: date | None,
    at_utc: datetime | None,
    text: str | None,
    zone: ZoneInfo,
) -> str | None:
    """An exact deadline in ``zone``, a date, a quoted unresolved phrase, or None."""
    if precision is DeadlinePrecision.DATETIME and at_utc is not None:
        return f"{at_utc.astimezone(zone):%Y-%m-%d %H:%M} ({zone.key})"
    if precision is DeadlinePrecision.DATE and deadline_date is not None:
        return deadline_date.isoformat()
    if precision is DeadlinePrecision.UNRESOLVED and text:
        return f'"{text}"'
    return None


def _deadline(item: DigestItem, zone: ZoneInfo) -> str | None:
    return _format_deadline(
        item.deadline_precision, item.deadline_date, item.deadline_at_utc, item.deadline_text, zone
    )


def _ownership(ownership: ActionOwnership) -> str:
    return "mine" if ownership is ActionOwnership.MINE else "waiting for"


def _print_suggestions(item: DigestItem, zone: ZoneInfo) -> None:
    """Pending suggestions with their IDs and steps, and accepted ones; never evidence."""
    for view in item.suggestions:
        suggestion = view.suggestion
        if view.state is SuggestionState.ACCEPTED:
            print(f"   [accepted] {suggestion.title}")
            continue
        if view.state is not SuggestionState.PENDING:
            continue  # Dismissed suggestions stay hidden.
        line = (
            f"   [pending #{view.suggestion_id}] {suggestion.title} "
            f"({_ownership(suggestion.ownership)})"
        )
        if suggestion.suggested_target_date is not None and suggestion.target_reason is not None:
            line += (
                f"; target {suggestion.suggested_target_date.isoformat()} "
                f"({TARGET_REASON_TEXT[suggestion.target_reason]})"
            )
        deadline = _format_deadline(
            suggestion.deadline_precision,
            suggestion.deadline_date,
            suggestion.deadline_at_utc,
            suggestion.deadline_text,
            zone,
        )
        if deadline is not None:
            line += f"; deadline {deadline}"
        print(line)
        for step in suggestion.steps:
            print(f"     - {step}")


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
        _print_suggestions(item, zone)


def _brief_exit_code(result: BriefRunResult) -> int:
    """0 only for a complete brief, or an empty one after a complete sync."""
    if result.status is BriefStatus.SAVED:
        complete = (DigestStatus.COMPLETE, DigestStatus.EMPTY)
        finished = result.digest is not None and result.digest.status in complete
        return 0 if finished and not _sync_incomplete(result) else 4
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
    """Sync, ask consent, analyze the shortlist with Groq and save today's brief."""
    settings = _load_settings()
    tz = resolve_timezone(timezone)
    now = datetime.now(UTC)
    window = local_day_window(now, tz)
    path = database_path or AppPaths.from_qt().database_path
    async with (
        gmail_provider(settings, silent_only=silent_only) as provider,
        groq_provider(settings) as ai,
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


async def actions(
    action: str,
    *,
    database_path: Path | None,
    view: str,
    timezone: str | None,
    suggestion_id: int | None,
) -> int:
    """List actions, or accept or dismiss a stored suggestion; needs no Gmail or AI access."""
    zone = resolve_timezone(timezone)
    path = database_path or AppPaths.from_qt().database_path
    await asyncio.to_thread(upgrade_database, path)
    database = Database.from_path(path)
    try:
        async with database.session() as session:
            service = ActionService(session)
            if action == "accept":
                assert suggestion_id is not None
                accepted = await service.accept(suggestion_id)
                print(f"Accepted: {accepted.title} ({accepted.public_id})")
                return 0
            if action == "dismiss":
                assert suggestion_id is not None
                await service.dismiss(suggestion_id)
                print("Dismissed.")
                return 0
            listed = await service.list_actions(ActionFilter(view))
            total = (
                await service.count_actions(ActionFilter.COMPLETED)
                if view == ActionFilter.COMPLETED.value and len(listed) >= COMPLETED_LIST_LIMIT
                else len(listed)
            )
    finally:
        await database.dispose()
    if not listed:
        print("No actions.")
        return 0
    now = datetime.now(UTC)
    today = now.astimezone(zone).date()
    for item in listed:
        deadline = _format_deadline(
            item.deadline_precision,
            item.deadline_date,
            item.deadline_at_utc,
            item.deadline_text,
            zone,
        )
        done = sum(step.done for step in item.steps)
        target = item.target_date.isoformat() if item.target_date is not None else "none"
        line = (
            f"{item.public_id} [{item.status.value}] {item.title} ({_ownership(item.ownership)}); "
            f"target {target}; deadline {deadline or 'none'}; steps {done}/{len(item.steps)}"
        )
        if item.status is ActionStatus.OPEN and item.carried_over(today, zone):
            line += "; carried over"
        if item.status is ActionStatus.OPEN and item.is_overdue(now):
            line += "; overdue"
        print(line)
    if total > len(listed):
        print(f"Showing the newest {len(listed)} of {total} completed actions.")
    return 0


def _terminal_safe(text: str) -> str:
    """Owner text without control characters that could drive a terminal; lines and tabs stay."""
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")


async def drafts(
    action: str,
    *,
    database_path: Path | None,
    public_id: str | None = None,
    output_format: str = "text",
    out: Path | None = None,
) -> int:
    """List drafts, or export one to stdout or a new file; needs no Gmail or AI access."""
    path = database_path or AppPaths.from_qt().database_path
    await asyncio.to_thread(upgrade_database, path)
    database = Database.from_path(path)
    try:
        async with database.session() as session:
            service = DraftService(session)
            if action == "list":
                summaries = await service.list_summaries()
            else:
                assert public_id is not None
                draft = await service.get(public_id)
    finally:
        await database.dispose()
    if action == "list":
        if not summaries:
            print("No drafts.")
            return 0
        zone = resolve_timezone(None)
        for summary in summaries:
            updated = summary.updated_at_utc.astimezone(zone).isoformat(timespec="minutes")
            title = clean_generated_text(summary.display_title)
            print(
                f"{summary.public_id} [{KIND_NAMES[summary.kind].lower()}] {title}; "
                f"updated {updated}; placeholders {summary.placeholder_count}"
            )
        return 0
    document = export_markdown(draft) if output_format == "markdown" else export_text(draft)
    if out is None:
        print(_terminal_safe(document), end="")
        return 0
    await asyncio.to_thread(write_text_atomically, out, document, overwrite=False)
    count = len(draft.placeholders)
    print(f"Exported to {out}." + (f" {still_to_fill(count)}" if count else ""))
    return 0


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
        help="Sync today's Inbox, analyze the shortlist with Groq after consent, save the brief.",
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
        help=(
            "Print the saved items (sender, subject, summary, action, deadline, link) "
            "and their pending or accepted suggestions."
        ),
    )
    key_parser = commands.add_parser(
        "ai-key", help="Save, check or remove the Groq API key in the OS credential store."
    )
    key_parser.add_argument(
        "action",
        choices=("set", "status", "clear"),
        help="set prompts without echoing; status never shows the key; clear is safe to repeat.",
    )
    consent_parser = commands.add_parser(
        "ai-consent", help="Show or revoke your recorded consent to send email to Groq."
    )
    consent_parser.add_argument(
        "action", choices=("status", "revoke"), help="Show or revoke consent for every account."
    )
    consent_parser.add_argument(
        "--database", type=Path, help="Optional SQLite path; defaults to app data."
    )
    actions_parser = commands.add_parser(
        "actions", help="List accepted actions, or accept or dismiss a suggestion."
    )
    action_commands = actions_parser.add_subparsers(dest="action", required=True)
    list_parser = action_commands.add_parser(
        "list", help="List open, waiting or completed actions."
    )
    list_parser.add_argument(
        "--view",
        choices=[item.value for item in ActionFilter],
        default=ActionFilter.OPEN.value,
        help="open (yours), waiting (on others) or completed; default open.",
    )
    list_parser.add_argument("--timezone", help="IANA timezone; defaults to the system timezone.")
    accept_parser = action_commands.add_parser(
        "accept", help="Accept a pending suggestion shown by brief --show."
    )
    dismiss_parser = action_commands.add_parser(
        "dismiss", help="Dismiss a pending suggestion shown by brief --show."
    )
    for decide in (accept_parser, dismiss_parser):
        decide.add_argument(
            "suggestion_id", type=int, help="The number after # in brief --show output."
        )
    for subcommand in (list_parser, accept_parser, dismiss_parser):
        subcommand.add_argument(
            "--database", type=Path, help="Optional SQLite path; defaults to app data."
        )
    drafts_parser = commands.add_parser(
        "drafts", help="List local drafts and notes, or export one. Nothing is sent."
    )
    draft_commands = drafts_parser.add_subparsers(dest="action", required=True)
    drafts_list = draft_commands.add_parser("list", help="List drafts, newest first.")
    drafts_export = draft_commands.add_parser(
        "export", help="Print a draft, or write it to a new file with --out."
    )
    drafts_export.add_argument("public_id", help="The draft ID shown by drafts list.")
    drafts_export.add_argument(
        "--format", choices=("text", "markdown"), default="text", help="Default text."
    )
    drafts_export.add_argument(
        "--out", type=Path, help="Write to this new file; an existing file is never replaced."
    )
    for subcommand in (drafts_list, drafts_export):
        subcommand.add_argument(
            "--database", type=Path, help="Optional SQLite path; defaults to app data."
        )
    args = parser.parse_args(arguments)
    # Wire/debug logging can expose authorization headers, loopback URLs and request bodies.
    for name in ("httpx", "httpcore", "groq"):
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
        if args.command == "actions":
            return asyncio.run(
                actions(
                    args.action,
                    database_path=args.database,
                    view=getattr(args, "view", ActionFilter.OPEN.value),
                    timezone=getattr(args, "timezone", None),
                    suggestion_id=getattr(args, "suggestion_id", None),
                )
            )
        if args.command == "drafts":
            return asyncio.run(
                drafts(
                    args.action,
                    database_path=args.database,
                    public_id=getattr(args, "public_id", None),
                    output_format=getattr(args, "format", "text"),
                    out=getattr(args, "out", None),
                )
            )
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
    except SettingsError as exc:
        print(str(exc))
        return 3
    except ConfigurationError as exc:
        # AI setup errors (model, key, vault) carry static, actionable messages.
        print(str(exc) if args.command in _OWN_MESSAGE_COMMANDS else _SETUP_UNAVAILABLE)
        return 3
    except ValidationError as exc:
        if args.command in _OWN_MESSAGE_COMMANDS:
            # Settings errors arrive as SettingsError, so this is an internal validation error.
            print(f"Unexpected error ({type(exc).__name__}).")
            return 1
        print(_SETUP_UNAVAILABLE)
        return 3
    except ProviderError as exc:
        print(str(exc))
        return 4
    except DraftNotFoundError:
        print(_DRAFT_NOT_FOUND)
        return 3
    except FileExistsError:
        print(_FILE_EXISTS)
        return 3
    except (ActionConflictError, ActionNotFoundError, SuggestionNotFoundError):
        # Before ValueError: ActionConflictError is one. Messages stay static.
        print(_ACTION_REFUSED)
        return 3
    except (InvalidTimezoneError, ShortlistReviewError):
        if args.command == "actions":
            print("Invalid timezone; use an IANA name such as America/Toronto.")
        else:
            print(
                "Invalid timezone or shortlist choices; "
                "IDs must belong to today's Inbox without overlap."
            )
        return 3
    except ValueError as exc:
        print(f"Unexpected error ({type(exc).__name__}).")
        return 1
    except (SQLAlchemyError, OSError, CommandError):
        print(
            "Local database or file operation failed. "
            "Check access and retry; no mailbox changes made."
        )
        return 5
    except KeyboardInterrupt:
        print("Cancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
