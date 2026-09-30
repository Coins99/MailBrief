"""Gmail connection checks, daily sync, body review, the consented AI brief, actions, local
drafts and consented AI drafting."""

import argparse
import asyncio
import contextlib
import getpass
import logging
import threading
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from alembic.util import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from mailbrief.config import Settings
from mailbrief.domain.actions import (
    TARGET_REASON_TEXT,
    Action,
    ActionFilter,
    ActionProposal,
    ActionStatus,
    ProposalState,
    SuggestionState,
    ThreadActivity,
    ThreadLink,
)
from mailbrief.domain.analysis import ActionOwnership, DeadlinePrecision, FollowUpKind
from mailbrief.domain.bodies import BodyStatus, MessageBody, PreparedBody
from mailbrief.domain.briefs import BriefRunResult, BriefStatus, TransmissionPreview
from mailbrief.domain.digests import DailyDigest, DigestItem, DigestStatus, SyncResult, SyncStatus
from mailbrief.domain.drafting import (
    DraftContextPart,
    DraftingOptions,
    DraftingOutcome,
    DraftingPreview,
    DraftingStatus,
)
from mailbrief.domain.drafts import (
    KIND_NAMES,
    DraftLength,
    DraftTone,
    export_markdown,
    export_text,
    placeholders,
    still_to_fill,
)
from mailbrief.domain.messages import ProviderKind, RankReason
from mailbrief.domain.preferences import OwnerPreferences, sender_excluded
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
from mailbrief.services.calendar import (
    InvalidTimezoneError,
    day_window,
    local_day_window,
    resolve_timezone,
)
from mailbrief.services.digest import DigestService
from mailbrief.services.drafting import (
    DRAFTING_DISCLOSURE_VERSION,
    DRAFTING_SCOPE,
    DraftingContextError,
    DraftingService,
)
from mailbrief.services.drafting import disclosure_lines as drafting_disclosure_lines
from mailbrief.services.drafts import DraftNotFoundError, DraftService
from mailbrief.services.history import (
    BriefDateError,
    BriefHistory,
    check_brief_date,
    coverage_line,
    parse_brief_date,
)
from mailbrief.services.preferences import (
    PreferencesService,
    PreferencesUnavailableError,
    ai_limits,
    effective_settings,
    owner_zone,
)
from mailbrief.services.proposals import ProposalNotFoundError, ProposalService
from mailbrief.services.ranking import (
    OUTSIDE_REPLY_TEXT,
    ExcludedSenderError,
    ShortlistReviewError,
    reason_text,
)
from mailbrief.services.threads import ThreadService
from mailbrief.storage.database import Database
from mailbrief.storage.migrate import upgrade_database
from mailbrief.storage.repositories import (
    AccountRepository,
    ConsentRepository,
    MessageRepository,
    OwnerConsentRepository,
    SyncRunRepository,
)
from mailbrief.text.prepare import clean_generated_text

_AI_COMMANDS = frozenset({"brief", "ai-key", "ai-consent"})
# Commands whose setup errors are shown as they are: static, actionable messages.
_OWN_MESSAGE_COMMANDS = _AI_COMMANDS | {"actions", "drafts", "preferences", "briefs"}
_NO_BRIEF = "No saved brief for that date."
_SEVERAL_BRIEFS = "Several accounts have a brief for that date; pass --account."
_TIMEZONE_HELP = "IANA time zone; defaults to your saved preference, else the system time zone."
_LIMIT_LABELS = {
    "ai_batch_size": "Messages per AI request",
    "ai_body_character_limit": "Body characters sent",
    "ai_max_output_tokens": "Output tokens",
    "ai_max_requests_per_run": "Requests per run",
    "ai_timeout_seconds": "Timeout (seconds)",
}
_ACTION_REFUSED = "That suggestion or action was not found or cannot change now."
_PROPOSAL_DECIDED = "That proposal was already applied or dismissed."
_PROPOSAL_ACTIONS = frozenset({"proposals", "apply-proposal", "dismiss-proposal"})
# Commands that show local times, in --timezone, else the saved zone, else the system's.
_ZONED_ACTIONS = frozenset({"list", *_PROPOSAL_ACTIONS})
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
    "AI_OUTPUT_INCOMPLETE": (
        "Groq couldn't finish some answers within the output limit, so they were left out. "
        "Try again, or raise MAILBRIEF_AI_MAX_OUTPUT_TOKENS."
    ),
    "AI_INVALID_OUTPUT": "Groq's answer couldn't be used; the draft is unchanged. Try again.",
    "AI_REFUSED": (
        "Groq declined to write this draft. Change the instructions or context and try again."
    ),
    "DRAFT_CHANGED": "The draft changed before Groq's text could be used; nothing was lost.",
    "ANALYSIS_FAILED": "No message could be analyzed.",
}
_DRAFTING_PARTS = {
    "use_email": DraftContextPart.SOURCE_EMAIL,
    "use_action": DraftContextPart.ACTION,
    "use_text": DraftContextPart.CURRENT_TEXT,
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


async def _preferences(path: Path) -> OwnerPreferences:
    """The owner's saved preferences; a missing database gives the defaults without
    creating it. Raises PreferencesUnavailableError when they can't be read."""
    if not await asyncio.to_thread(path.exists):
        return OwnerPreferences.defaults()
    await asyncio.to_thread(upgrade_database, path)
    database = Database.from_path(path)
    try:
        async with database.session() as session:
            return await PreferencesService(session).get()
    finally:
        await database.dispose()


@dataclass(frozen=True, slots=True)
class _Owner:
    """What a command needs before it builds any provider."""

    path: Path
    preferences: OwnerPreferences
    settings: Settings
    zone: ZoneInfo


async def _owner(database_path: Path | None, timezone: str | None) -> _Owner:
    """Settings, then the owner's preferences and zone, before any provider is built.

    Invalid settings or an unknown --timezone fail before the database is touched. Saved
    AI limits apply where no MAILBRIEF_* variable is set, and --timezone wins over the
    saved zone, which wins over the system time zone.
    """
    settings = _load_settings()
    explicit = resolve_timezone(timezone) if timezone and timezone.strip() else None
    path = database_path or AppPaths.from_qt().database_path
    preferences = await _preferences(path)
    return _Owner(
        path=path,
        preferences=preferences,
        settings=effective_settings(settings, preferences),
        zone=explicit or owner_zone(preferences),
    )


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
    owner = await _owner(database_path, timezone)
    rules = owner.preferences.excluded_senders
    now = datetime.now(UTC)
    window = local_day_window(now, owner.zone)
    path = owner.path
    async with gmail_provider(owner.settings, silent_only=silent_only) as provider:
        await provider.connect()
        await asyncio.to_thread(upgrade_database, path)
        database = Database.from_path(path)
        try:
            async with database.session() as session:
                accounts = AccountRepository(session)
                messages = MessageRepository(session)
                threads = ThreadService(session, provider)
                application = ApplicationService(
                    provider,
                    messages,
                    SyncRunRepository(session),
                    accounts,
                    threads=threads,
                )
                result, shortlist = await application.prepare_daily_shortlist(
                    tz_key=owner.zone.key,
                    now_utc=now,
                    include_ids=include_ids,
                    exclude_ids=exclude_ids,
                    shortlist_limit=owner.preferences.shortlist_limit,
                    excluded_senders=rules,
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
                _print_threads(result)
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
                        marker = (
                            "excluded"
                            if sender_excluded(row.sender_address, rules)
                            else "selected"
                            if row.provider_message_id in selected
                            else "omitted"
                        )
                        print(
                            f"{row.provider_message_id} [{marker}] score={row.rank_score} "
                            f"{row.sender_address}: {row.subject}"
                        )
                        reasons = ", ".join(_reason_words(value) for value in row.rank_reasons_json)
                        if reasons:
                            print(f"  reasons: {reasons}")
                        print(f"  {row.web_link}")
                    # Replies in tracked threads that today's Inbox sync can't see (ADR 0016).
                    # Excluded senders never appear, like in the review.
                    for reply in await threads.outside_replies(
                        account, window, excluded_senders=rules
                    ):
                        key = reply.provider_message_id
                        stored = await messages.get_by_provider_message_id(account.id, key)
                        score = None if stored is None else stored.rank_score
                        marker = "selected" if key in selected else "omitted"
                        print(
                            f"{key} [{marker}] score={score} {reply.sender.address}: "
                            f"{reply.subject}"
                        )
                        print(f"  {OUTSIDE_REPLY_TEXT}")
                        print(f"  {reply.web_link}")
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
    owner = await _owner(database_path, timezone)
    settings, tz, path = owner.settings, owner.zone, owner.path
    now = datetime.now(UTC)
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
                    tz_key=tz.key,
                    now_utc=now,
                    include_ids=include_ids,
                    exclude_ids=exclude_ids,
                    shortlist_limit=owner.preferences.shortlist_limit,
                    excluded_senders=owner.preferences.excluded_senders,
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


def _granted(when: datetime | None) -> str:
    return f"granted {when:%Y-%m-%d %H:%M} UTC" if when is not None else "not granted"


async def ai_consent(action: str, *, database_path: Path | None) -> int:
    """Show or revoke recorded Groq consent: each account's for briefs, and the owner's for
    AI drafting. Needs no Gmail connection."""
    path = database_path or AppPaths.from_qt().database_path
    await asyncio.to_thread(upgrade_database, path)
    database = Database.from_path(path)
    try:
        async with database.session() as session:
            accounts = await AccountRepository(session).list_all()
            consents = ConsentRepository(session)
            owner = OwnerConsentRepository(session)
            if action == "status":
                if not accounts:
                    print("No accounts in this database.")
                for account in accounts:
                    active = await consents.get_active(
                        account.id, PROVIDER_NAME, CONSENT_DISCLOSURE_VERSION
                    )
                    when = None if active is None else active.granted_at_utc
                    print(f"Account {account.id}: Groq consent {_granted(when)}")
                drafting = await owner.get_active(
                    PROVIDER_NAME, DRAFTING_SCOPE, DRAFTING_DISCLOSURE_VERSION
                )
                when = None if drafting is None else drafting.granted_at_utc
                print(f"AI drafting: Groq consent {_granted(when)}")
                return 0
            now = datetime.now(UTC)
            briefs = 0
            for account in accounts:
                briefs += await consents.revoke_all(account.id, PROVIDER_NAME, now)
            drafting_revoked = await owner.revoke_all(PROVIDER_NAME, now)
            await session.commit()
            print(
                f"Groq consent revoked: {briefs + drafting_revoked} "
                f"(briefs {briefs}, drafting {drafting_revoked})"
            )
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


class CliDraftingGate:
    """Terminal approval for AI drafting: the same rules as the brief's consent."""

    def __init__(self, *, assume_yes: bool) -> None:
        self._assume_yes = assume_yes

    async def request_drafting_consent(self, preview: DraftingPreview) -> bool:
        for line in drafting_disclosure_lines(preview):
            print(line)
        if preview.first_use:
            if self._assume_yes:
                print(
                    "First use needs your interactive consent; run drafts generate without --yes."
                )
                return False
            return await _ask('Type "yes" to consent and send: ') == "yes"
        if self._assume_yes:
            return True
        return (await _ask("Send? [y/N] ")).lower() in {"y", "yes"}


class _GmailBodies:
    """Opens Gmail for one body download, then closes it; only --use-email needs it."""

    def __init__(self, settings: Settings, *, silent_only: bool) -> None:
        self._settings = settings
        self._silent_only = silent_only

    async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
        async with gmail_provider(self._settings, silent_only=self._silent_only) as provider:
            return await provider.fetch_message_body(provider_message_id)


def _drafting_exit_code(outcome: DraftingOutcome) -> int:
    """Brief's codes: 0 done, 6 declined, 130 cancelled, 4 failed."""
    if outcome.status is DraftingStatus.GENERATED:
        return 0
    if outcome.status is DraftingStatus.DECLINED:
        return 6
    if outcome.status is DraftingStatus.CANCELLED:
        return 130
    return 4


async def drafts_generate(
    public_id: str,
    *,
    database_path: Path | None,
    parts: frozenset[DraftContextPart],
    tone: str | None,
    length: str | None,
    instructions: str,
    assume_yes: bool,
    silent_only: bool,
) -> int:
    """Write a new version of a draft with Groq after the owner approves what is sent.

    Tone and length default to the saved preferences, and an excluded sender's email is
    never offered.
    """
    owner = await _owner(database_path, None)
    settings, path, preferences = owner.settings, owner.path, owner.preferences
    await asyncio.to_thread(upgrade_database, path)
    database = Database.from_path(path)
    bodies = (
        BodyService(
            _GmailBodies(settings, silent_only=silent_only),
            limit=settings.ai_body_character_limit,
        )
        if DraftContextPart.SOURCE_EMAIL in parts
        else None
    )
    try:
        async with groq_provider(settings) as ai, database.session() as session:
            service = DraftingService(
                session,
                ai,
                bodies,
                zone=owner.zone,
                excluded_senders=preferences.excluded_senders,
            )
            options = DraftingOptions(
                parts=parts,
                tone=preferences.draft_tone if tone is None else DraftTone(tone),
                length=preferences.draft_length if length is None else DraftLength(length),
                instructions=instructions,
            )
            plan = await service.prepare(public_id, options)
            outcome = await service.generate(plan, CliDraftingGate(assume_yes=assume_yes))
    finally:
        await database.dispose()
    if outcome.status is DraftingStatus.GENERATED:
        assert outcome.draft is not None
        previous = (
            f"; your previous text is version v{outcome.previous_version}"
            if outcome.previous_version is not None
            else ""
        )
        print(f"New version v{outcome.version_number} from Groq{previous}.")
        found = placeholders(
            "\n".join(
                (
                    outcome.draft.to_text,
                    outcome.draft.cc_text,
                    outcome.draft.title,
                    outcome.draft.body,
                )
            )
        )
        print("Placeholders: " + (", ".join(found) if found else "none"))
        for item in outcome.missing_context:
            print(f"Missing context: {item}")
        print(f"See the text with: mailbrief-gmail-diagnostic drafts export {public_id}")
    elif outcome.status is DraftingStatus.DECLINED:
        print("Nothing was sent. The draft is unchanged.")
    elif outcome.status is DraftingStatus.CANCELLED:
        print("Cancelled. The draft is unchanged.")
    else:
        code = outcome.error_code or "AI_PROVIDER_ERROR"
        print(_AI_ERROR_MESSAGES.get(code, "Groq couldn't write the draft."))
        if outcome.provider_detail:
            print(f"{provider_display_name(PROVIDER_NAME)} detail: {outcome.provider_detail}")
        print("The draft is unchanged.")
    return _drafting_exit_code(outcome)


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


def _print_items(
    digest: DailyDigest,
    links: Mapping[str, tuple[ThreadLink, ...]] | None = None,
    proposals: Mapping[str, tuple[ActionProposal, ...]] | None = None,
) -> None:
    """Derived brief content, shown only on request; never evidence or bodies.

    ``links`` names the open actions that continue each item's thread; an action the email
    is already a source of isn't repeated. ``proposals`` names each item's pending follow-up
    proposals, without their quotes.
    """
    zone = ZoneInfo(digest.timezone_name)
    for item in digest.items:
        print(f"{item.position + 1}. [{item.section.value}] {item.sender.address}: {item.subject}")
        print(f"   {item.summary}")
        for link in (links or {}).get(item.message_key, ()):
            if not link.is_source:
                print(f"   Continues: {_terminal_safe(link.title)} ({link.public_id})")
        for proposal in (proposals or {}).get(item.message_key, ()):
            title = _terminal_safe(proposal.action_title)
            print(f"   Proposes: {_proposal_kind(proposal, zone)} for {title} (P{proposal.id})")
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
    date_text: str | None = None,
) -> int:
    """Sync, ask consent, analyze the shortlist with Groq and save the day's brief.

    The day is today, or ``date_text``: today or one of the previous seven days, checked
    before Gmail is contacted.
    """
    day = None if date_text is None else parse_brief_date(date_text)
    owner = await _owner(database_path, timezone)
    settings, tz, path = owner.settings, owner.zone, owner.path
    now = datetime.now(UTC)
    today = local_day_window(now, tz).local_date
    check_brief_date(day or today, today)
    window = day_window(day or today, tz)
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
                        threads=ThreadService(session, provider),
                    ),
                    bodies=BodyService(provider, limit=settings.ai_body_character_limit),
                    analysis=AnalysisService(session, ai, batch_size=settings.ai_batch_size),
                    digests=DigestService(session),
                    consent_gate=CliConsentGate(assume_yes=assume_yes),
                    clock=lambda: now,
                )
                result = await service.generate(
                    tz_key=tz.key,
                    include_ids=include_ids,
                    exclude_ids=exclude_ids,
                    shortlist_limit=owner.preferences.shortlist_limit,
                    excluded_senders=owner.preferences.excluded_senders,
                    local_date=window.local_date,
                )
                links, proposals = (
                    await _brief_extras(session, result.digest)
                    if show and result.digest is not None
                    else ({}, {})
                )
        finally:
            await database.dispose()
        model = ai.model_name
    print(f"Inbox date: {window.local_date} ({window.timezone_name})")
    _print_result(result, model=model)
    _print_threads(result.sync)
    created = result.proposals_created
    if created:
        print(f"Proposed {created} {'update' if created == 1 else 'updates'} to your actions.")
    if show and result.digest is not None:
        print(coverage_line(result.digest))
        _print_items(result.digest, links, proposals)
    return _brief_exit_code(result)


async def _brief_extras(
    session: AsyncSession, digest: DailyDigest
) -> tuple[dict[str, tuple[ThreadLink, ...]], dict[str, tuple[ActionProposal, ...]]]:
    """The open actions that continue each item's thread, and each item's pending
    proposals, read from local data."""
    keys = [item.message_key for item in digest.items]
    links = await ActionService(session).thread_links(digest.account_id, keys)
    proposals = await ProposalService(session).pending_for_messages(digest.account_id, keys)
    return links, proposals


async def _open_existing(path: Path) -> Database | None:
    """The migrated database, or None when it doesn't exist; it is never created here."""
    if not await asyncio.to_thread(path.exists):
        return None
    await asyncio.to_thread(upgrade_database, path)
    return Database.from_path(path)


async def briefs_list(
    *, database_path: Path | None, limit: int, timezone: str | None = None
) -> int:
    """Saved briefs, newest day first, then each Gmail account's missed days; offline.

    Missed days are the previous seven days without a brief, in --timezone, else the saved
    time zone, else the system's.
    """
    explicit = resolve_timezone(timezone) if timezone and timezone.strip() else None
    path = database_path or AppPaths.from_qt().database_path
    database = await _open_existing(path)
    if database is None:
        print("No saved briefs.")
        return 0
    try:
        async with database.session() as session:
            if explicit is not None:
                zone = explicit
            else:
                try:
                    zone = owner_zone(await PreferencesService(session).get())
                except PreferencesUnavailableError:
                    await session.rollback()
                    print("Saved preferences could not be read; using the system time zone.")
                    zone = resolve_timezone(None)
            today = datetime.now(UTC).astimezone(zone).date()
            history = BriefHistory(session)
            summaries = await history.list_saved(limit)
            accounts = [
                account.email_address
                for account in await AccountRepository(session).list_all()
                if account.provider == ProviderKind.GMAIL.value
            ]
            missed = {email: await history.missed_days(email, today) for email in accounts}
    finally:
        await database.dispose()
    if not summaries:
        print("No saved briefs.")
    for summary in summaries:
        noun = "item" if summary.item_count == 1 else "items"
        print(
            f"{summary.local_date} {summary.account_email}: {summary.status.value}; "
            f"{summary.item_count} {noun}"
        )
        print(f"  {coverage_line(summary)}")
    for email, days in missed.items():
        listed = ", ".join(day.isoformat() for day in days) or "none"
        print(f"Missed days for {email} (last 7 days): {listed}")
    return 0


async def briefs_show(date_text: str, *, account: str | None, database_path: Path | None) -> int:
    """Print one saved brief as brief --show does, with its coverage line; offline."""
    day = parse_brief_date(date_text)
    path = database_path or AppPaths.from_qt().database_path
    database = await _open_existing(path)
    if database is None:
        print(_NO_BRIEF)
        return 3
    try:
        async with database.session() as session:
            history = BriefHistory(session)
            if account is None:
                accounts = await history.accounts_for(day)
                if len(accounts) > 1:
                    print(_SEVERAL_BRIEFS)
                    return 3
                account = accounts[0] if accounts else None
            digest = None if account is None else await history.get(account, day)
            links, proposals = ({}, {}) if digest is None else await _brief_extras(session, digest)
    finally:
        await database.dispose()
    if digest is None:
        print(_NO_BRIEF)
        return 3
    print(
        f"Brief for {digest.local_date} ({digest.account_id}): {digest.status.value}; "
        f"items: {len(digest.items)}"
    )
    print(coverage_line(digest))
    _print_items(digest, links, proposals)
    return 0


def _print_threads(result: SyncResult) -> None:
    """The thread check's counts, when any threads were tracked or the check stopped."""
    if not (result.threads_tracked or result.threads_stopped_code):
        return
    saved = result.thread_messages
    line = (
        f"Tracked threads: {result.threads_checked} checked, {result.threads_failed} failed, "
        f"{saved} {'message' if saved == 1 else 'messages'} saved"
    )
    if result.threads_stopped_code:
        line += f"; stopped: {result.threads_stopped_code}"
    print(line)


def _thread_text(thread: ThreadActivity | None, zone: ZoneInfo) -> str:
    """Later messages in an action's threads, in the owner's zone; empty when none."""
    if thread is None:
        return ""
    text = ""
    if thread.latest_at_utc is not None and thread.latest_sender is not None:
        latest = thread.latest_at_utc.astimezone(zone)
        text += (
            f"; thread: {thread.new_messages} new, latest {latest:%Y-%m-%d %H:%M} from "
            f"{_terminal_safe(thread.latest_sender)}"
        )
    if thread.owner_replied_at_utc is not None:
        text += f"; you replied {thread.owner_replied_at_utc.astimezone(zone).date().isoformat()}"
    return text


async def actions(
    action: str,
    *,
    database_path: Path | None,
    view: str,
    timezone: str | None,
    suggestion_id: int | None,
    public_id: str | None = None,
    into: str | None = None,
    proposal_id: int | None = None,
) -> int:
    """List actions, accept or dismiss a stored suggestion, mark an action's threads seen,
    or list, apply or dismiss follow-up proposals; needs no Gmail or AI access.

    ``into`` accepts the suggestion into that existing action instead of creating one. Lists
    show dates in --timezone, else the saved time zone. They only display local data, so
    unreadable preferences fall back to the system time zone.
    """
    zone = resolve_timezone(timezone) if timezone and timezone.strip() else None
    path = database_path or AppPaths.from_qt().database_path
    await asyncio.to_thread(upgrade_database, path)
    database = Database.from_path(path)
    try:
        async with database.session() as session:
            if zone is None and action in _ZONED_ACTIONS:
                try:
                    zone = owner_zone(await PreferencesService(session).get())
                except PreferencesUnavailableError:
                    await session.rollback()
                    print("Saved preferences could not be read; showing the system time zone.")
            if action in _PROPOSAL_ACTIONS:
                return await _proposals_command(
                    session, action, proposal_id, zone or resolve_timezone(None)
                )
            service = ActionService(session)
            if action == "accept" and into is not None:
                assert suggestion_id is not None
                current = await service.get(into)
                try:
                    added = await service.accept_into(suggestion_id, into, current.revision)
                except ActionConflictError as exc:
                    print(str(exc))  # Static, such as a suggestion owned by another action.
                    return 3
                title = _terminal_safe(added.action.title)
                print(f"Added to: {title} ({added.action.public_id})")
                return 0
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
            if action == "seen":
                assert public_id is not None
                current = await service.get(public_id)
                marked = await service.mark_thread_seen(public_id, current.revision)
                unchanged = marked.revision == current.revision
                print("Nothing new in its threads." if unchanged else "Marked seen.")
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
    zone = zone or resolve_timezone(None)
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
        line += _thread_text(item.thread, zone)
        if item.proposals:
            count = len(item.proposals)
            line += f"; {count} {'proposal' if count == 1 else 'proposals'}"
        print(line)
    if total > len(listed):
        print(f"Showing the newest {len(listed)} of {total} completed actions.")
    return 0


def _reason_words(value: str) -> str:
    """A stored rank reason in words; one this version doesn't know is shown as stored."""
    try:
        return reason_text(RankReason(value))
    except ValueError:
        return value.replace("_", " ")


def _proposal_id(text: str) -> int:
    """A proposal ID as actions proposals shows it (P3), or its number alone."""
    value = text.strip().removeprefix("P").removeprefix("p")
    if not value.isdigit():
        raise argparse.ArgumentTypeError("use the ID shown by actions proposals, such as P3")
    return int(value)


def _proposal_kind(proposal: ActionProposal, zone: ZoneInfo) -> str:
    """What a proposal would do, in words; a new deadline is shown in ``zone``."""
    if proposal.kind is not FollowUpKind.NEW_DEADLINE:
        return proposal.kind.value
    deadline = _format_deadline(
        proposal.deadline_precision,
        proposal.deadline_date,
        proposal.deadline_at_utc,
        proposal.deadline_text,
        zone,
    )
    return _terminal_safe(f"new deadline {deadline}")


def _proposal_line(proposal: ActionProposal, zone: ZoneInfo) -> str:
    """One pending proposal on one line: its ID, action, kind, quote and email."""
    received = proposal.received_at_utc.astimezone(zone).date().isoformat()
    parts = (
        f"P{proposal.id}",
        f"{proposal.action_public_id} {proposal.action_title}",
        _proposal_kind(proposal, zone),
        f'"{" ".join(proposal.evidence.split())}"',
        f"{proposal.sender_address}, {received}, {' '.join(proposal.subject.split())}",
    )
    return _terminal_safe(" · ".join(parts))


def _applied_line(proposal: ActionProposal, before: Action, after: Action, zone: ZoneInfo) -> str:
    """What applying a proposal changed."""
    name = f"{after.title} ({after.public_id})"
    if proposal.kind is not FollowUpKind.NEW_DEADLINE:
        line = f"Applied P{proposal.id}: completed {name}; the email says it was {proposal.kind}."
        return _terminal_safe(line)
    deadline = _format_deadline(
        after.deadline_precision,
        after.deadline_date,
        after.deadline_at_utc,
        after.deadline_text,
        zone,
    )
    line = f"Applied P{proposal.id}: {name} now has deadline {deadline}"
    if before.target_date == before.suggested_target_date:
        target = "none" if after.target_date is None else after.target_date.isoformat()
        line += f"; target date {target}."
    else:
        line += "; the target date you set is unchanged."
    return _terminal_safe(line)


async def _proposals_command(
    session: AsyncSession, action: str, proposal_id: int | None, zone: ZoneInfo
) -> int:
    """actions proposals, apply-proposal and dismiss-proposal; local data only."""
    service = ProposalService(session)
    if action == "proposals":
        pending = await service.pending()
        if not pending:
            print("No pending proposals.")
        for proposal in pending:
            print(_proposal_line(proposal, zone))
        return 0
    assert proposal_id is not None
    proposal = await service.get(proposal_id)
    if proposal.state is not ProposalState.PENDING:
        print(_PROPOSAL_DECIDED)
        return 3
    if action == "dismiss-proposal":
        await service.dismiss(proposal_id)
        print(f"Dismissed P{proposal_id}; it won't be proposed again.")
        return 0
    before = await ActionService(session).get(proposal.action_public_id)
    try:
        after = await service.apply(proposal_id, before.revision)
    except ActionConflictError as exc:
        print(str(exc))  # Static, such as an action that is no longer open.
        return 3
    print(_applied_line(proposal, before, after, zone))
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


def _number(value: float) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


async def preferences_show(*, database_path: Path | None) -> int:
    """Print the owner's preferences and where each AI limit comes from; no network.

    Sender rules are the owner's own text and are shown here, to the owner only.
    """
    settings = _load_settings()
    path = database_path or AppPaths.from_qt().database_path
    exists = await asyncio.to_thread(path.exists)
    preferences = await _preferences(path)
    if not exists:
        print("No database yet: these are the defaults.")
    elif preferences.updated_at_utc is None:
        print("Never saved: these are the defaults.")
    else:
        print(f"Saved {preferences.updated_at_utc:%Y-%m-%d %H:%M} UTC.")
    zone = preferences.time_zone or f"system ({owner_zone(preferences).key})"
    print(f"Time zone: {zone}")
    print(f"Messages per brief: {preferences.shortlist_limit}")
    rules = preferences.excluded_senders
    print(f"Excluded senders: {len(rules) or 'none'}")
    for rule in rules:
        print(f"  {rule}")
    print(
        f"Drafting defaults: tone {preferences.draft_tone.value}, "
        f"length {preferences.draft_length.value}"
    )
    print("AI limits (a MAILBRIEF_AI_* variable wins over a saved value):")
    for limit in ai_limits(settings, preferences):
        print(f"  {_LIMIT_LABELS[limit.name]}: {_number(limit.value)} ({limit.source})")
    return 0


def _brief_count(text: str) -> int:
    """A --limit of 1 to 365 briefs."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("use a whole number from 1 to 365") from None
    if not 1 <= value <= 365:
        raise argparse.ArgumentTypeError("use a whole number from 1 to 365")
    return value


def _add_day_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--silent-only", action="store_true", help="Never open a browser.")
    parser.add_argument("--database", type=Path, help="Optional SQLite path; defaults to app data.")
    parser.add_argument("--timezone", help=_TIMEZONE_HELP)
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
        "--date",
        dest="date_text",
        metavar="YYYY-MM-DD",
        help="Brief this day instead of today: one of the previous 7 days, never automatic.",
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
        "actions",
        help=(
            "List accepted actions, accept or dismiss a suggestion, or decide follow-up proposals."
        ),
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
    list_parser.add_argument("--timezone", help=_TIMEZONE_HELP)
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
    accept_parser.add_argument(
        "--into",
        metavar="PUBLIC_ID",
        help="Add the suggestion's email to this existing action instead of creating one.",
    )
    seen_parser = action_commands.add_parser(
        "seen", help="Mark the later messages in an action's threads as seen."
    )
    seen_parser.add_argument("public_id", help="The action ID shown by actions list.")
    proposals_parser = action_commands.add_parser(
        "proposals", help="List pending proposals to update your actions from later emails."
    )
    apply_proposal_parser = action_commands.add_parser(
        "apply-proposal", help="Apply a proposal shown by actions proposals."
    )
    dismiss_proposal_parser = action_commands.add_parser(
        "dismiss-proposal", help="Dismiss a proposal; it won't be proposed again."
    )
    for decide in (apply_proposal_parser, dismiss_proposal_parser):
        decide.add_argument(
            "proposal_id",
            type=_proposal_id,
            metavar="ID",
            help="The ID shown by actions proposals, such as P3.",
        )
    for shown in (proposals_parser, apply_proposal_parser):
        shown.add_argument("--timezone", help=_TIMEZONE_HELP)
    for subcommand in (
        list_parser,
        accept_parser,
        dismiss_parser,
        seen_parser,
        proposals_parser,
        apply_proposal_parser,
        dismiss_proposal_parser,
    ):
        subcommand.add_argument(
            "--database", type=Path, help="Optional SQLite path; defaults to app data."
        )
    drafts_parser = commands.add_parser(
        "drafts",
        help="List or export local drafts and notes, or write one with Groq after approval.",
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
    drafts_generate_parser = draft_commands.add_parser(
        "generate",
        help="Write a new version with Groq from the parts you choose, after you approve them.",
    )
    drafts_generate_parser.add_argument("public_id", help="The draft ID shown by drafts list.")
    drafts_generate_parser.add_argument(
        "--use-email",
        action="store_true",
        help="Send the email being replied to (downloaded from Gmail now, never saved).",
    )
    drafts_generate_parser.add_argument(
        "--use-action", action="store_true", help="Send the linked action."
    )
    drafts_generate_parser.add_argument(
        "--use-text", action="store_true", help="Send the draft's current title and body."
    )
    drafts_generate_parser.add_argument(
        "--tone",
        choices=[tone.value for tone in DraftTone],
        help="Defaults to your saved drafting tone, else neutral.",
    )
    drafts_generate_parser.add_argument(
        "--length",
        choices=[length.value for length in DraftLength],
        help="Defaults to your saved drafting length, else medium.",
    )
    drafts_generate_parser.add_argument(
        "--instructions", default="", help="What you want written (at most 1,000 characters)."
    )
    drafts_generate_parser.add_argument(
        "--yes",
        action="store_true",
        help="Send without asking when consent is already recorded; first use still asks.",
    )
    drafts_generate_parser.add_argument(
        "--silent-only",
        action="store_true",
        help="With --use-email: never open a browser to sign in to Gmail.",
    )
    for subcommand in (drafts_list, drafts_export, drafts_generate_parser):
        subcommand.add_argument(
            "--database", type=Path, help="Optional SQLite path; defaults to app data."
        )
    briefs_parser = commands.add_parser(
        "briefs", help="List saved briefs and missed days, or show one saved brief; offline."
    )
    brief_commands = briefs_parser.add_subparsers(dest="action", required=True)
    briefs_list_parser = brief_commands.add_parser(
        "list", help="Saved briefs, newest first, then each account's missed days."
    )
    briefs_list_parser.add_argument(
        "--limit", type=_brief_count, default=30, help="How many briefs to list; default 30."
    )
    briefs_list_parser.add_argument("--timezone", help=_TIMEZONE_HELP)
    briefs_show_parser = brief_commands.add_parser(
        "show", help="Print one saved brief and what it covers."
    )
    briefs_show_parser.add_argument("date_text", metavar="DATE", help="The day, YYYY-MM-DD.")
    briefs_show_parser.add_argument(
        "--account", help="The Gmail address, when several accounts have a brief that day."
    )
    for subcommand in (briefs_list_parser, briefs_show_parser):
        subcommand.add_argument(
            "--database", type=Path, help="Optional SQLite path; defaults to app data."
        )
    preferences_parser = commands.add_parser(
        "preferences", help="Show your saved preferences; set them in the desktop's Settings."
    )
    preference_commands = preferences_parser.add_subparsers(dest="action", required=True)
    preferences_show_parser = preference_commands.add_parser(
        "show", help="Time zone, messages per brief, sender rules, drafting and AI limits."
    )
    preferences_show_parser.add_argument(
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
                    date_text=args.date_text,
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
                    public_id=getattr(args, "public_id", None),
                    into=getattr(args, "into", None),
                    proposal_id=getattr(args, "proposal_id", None),
                )
            )
        if args.command == "drafts" and args.action == "generate":
            return asyncio.run(
                drafts_generate(
                    args.public_id,
                    database_path=args.database,
                    parts=frozenset(
                        part for flag, part in _DRAFTING_PARTS.items() if getattr(args, flag)
                    ),
                    tone=args.tone,
                    length=args.length,
                    instructions=args.instructions,
                    assume_yes=args.yes,
                    silent_only=args.silent_only,
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
        if args.command == "preferences":
            return asyncio.run(preferences_show(database_path=args.database))
        if args.command == "briefs" and args.action == "list":
            return asyncio.run(
                briefs_list(database_path=args.database, limit=args.limit, timezone=args.timezone)
            )
        if args.command == "briefs":
            return asyncio.run(
                briefs_show(args.date_text, account=args.account, database_path=args.database)
            )
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
    except PreferencesUnavailableError as exc:
        print(str(exc))  # Static and actionable; nothing is sent while it lasts.
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
    except DraftingContextError as exc:
        print(str(exc))  # Static messages that name no mail content.
        return 3
    except FileExistsError:
        print(_FILE_EXISTS)
        return 3
    except ProposalNotFoundError as exc:
        print(str(exc))  # Static.
        return 3
    except (ActionConflictError, ActionNotFoundError, SuggestionNotFoundError):
        # Before ValueError: ActionConflictError is one. Messages stay static.
        print(_ACTION_REFUSED)
        return 3
    except ExcludedSenderError as exc:
        print(str(exc))  # Static: names no sender, rule or message.
        return 3
    except BriefDateError as exc:
        print(str(exc))  # Static: the allowed dates, never the text given.
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
