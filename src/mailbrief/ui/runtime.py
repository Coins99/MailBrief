"""Desktop composition; every operation owns and closes its provider/session resources."""

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import SecretStr

from mailbrief.config import Settings
from mailbrief.domain.actions import (
    Action,
    ActionEdit,
    ActionFilter,
    ActionProposal,
    StepEdit,
    ThreadLink,
)
from mailbrief.domain.bodies import MessageBody
from mailbrief.domain.briefs import (
    AutoSendPermission,
    AutoSendStatus,
    BriefRunResult,
    TransmissionPreview,
)
from mailbrief.domain.cached_mail import CachedAccount, CachedMailPage
from mailbrief.domain.digests import DailyDigest, SavedBriefSummary, SyncProgress
from mailbrief.domain.drafting import DraftContextPart, DraftingOptions, DraftingOutcome
from mailbrief.domain.drafts import (
    Draft,
    DraftEdit,
    DraftKind,
    DraftSummary,
    DraftVersion,
    DraftVersionInfo,
)
from mailbrief.domain.messages import ProviderKind
from mailbrief.domain.preferences import OwnerPreferences, PreferencesEdit
from mailbrief.providers.gmail.cache import GmailCredentialStore
from mailbrief.providers.gmail.factory import gmail_provider
from mailbrief.providers.gmail.oauth import DesktopClient
from mailbrief.providers.groq.credentials import GroqKeyStore
from mailbrief.providers.groq.factory import groq_provider
from mailbrief.providers.groq.provider import PRIVACY_NOTICE, PROVIDER_NAME
from mailbrief.services.actions import AcceptedInto, ActionService
from mailbrief.services.analysis import AnalysisService
from mailbrief.services.application import ApplicationService
from mailbrief.services.bodies import BodyService
from mailbrief.services.brief import (
    CONSENT_DISCLOSURE_VERSION,
    BriefService,
    ConsentGate,
    ShortlistGate,
    permission_preview,
)
from mailbrief.services.calendar import day_window, resolve_timezone
from mailbrief.services.consent import auto_send_permission, set_auto_send
from mailbrief.services.digest import DigestService
from mailbrief.services.drafting import (
    DRAFTING_DISCLOSURE_VERSION,
    DRAFTING_SCOPE,
    DraftingGate,
    DraftingPlan,
    DraftingService,
)
from mailbrief.services.drafts import DraftService
from mailbrief.services.history import BriefHistory, check_brief_date
from mailbrief.services.preferences import (
    PreferencesService,
    PreferencesUnavailableError,
    effective_settings,
    owner_zone,
)
from mailbrief.services.proposals import ProposalService
from mailbrief.services.threads import ThreadService
from mailbrief.storage.database import Database
from mailbrief.storage.migrate import upgrade_database
from mailbrief.storage.repositories import (
    AccountRepository,
    ConsentRepository,
    DigestRepository,
    MessageRepository,
    OwnerConsentRepository,
    SyncRunRepository,
)
from mailbrief.ui.preferences import DesktopPreferences, PreferencesStore


class _NeverAsks:
    """The consent gate of an automatic run, which has nobody to ask: BriefService never calls
    it, and if it ever did, nothing would be sent."""

    async def confirm(self, preview: object) -> bool:
        return False


class _GmailBodies:
    """Opens Gmail (silent sign-in only) for one body download, then closes it.

    AI drafting downloads a body only when the owner chooses the email, so nothing here
    touches Gmail until then.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
        async with gmail_provider(self._settings, silent_only=True) as provider:
            return await provider.fetch_message_body(provider_message_id)


class DesktopRuntime:
    """Use the same settings, vault, migrations and services as the diagnostic CLI.

    The owner's preferences are read for every operation that could send data, and
    unreadable preferences stop it (PreferencesUnavailableError) rather than fall back to
    defaults that would drop the owner's sender exclusions.
    """

    def __init__(self, database_path: Path) -> None:
        self._path = database_path
        self._database: Database | None = None
        self._preferences = PreferencesStore(database_path.parent / "desktop-settings.json")

    async def load_saved(self) -> DailyDigest | None:
        if self._database is None:

            def initialize() -> Database:
                upgrade_database(self._path)
                return Database.from_path(self._path)

            initialization = asyncio.create_task(asyncio.to_thread(initialize))
            try:
                self._database = await asyncio.shield(initialization)
            except asyncio.CancelledError:
                # A worker-thread migration cannot be cancelled. Join it before shutdown
                # or a retry can touch the database, and release its newly created engine.
                database = await initialization
                await database.dispose()
                raise
        async with self._database.session() as session:
            return await DigestRepository(session).get_latest()

    async def connect(self, *, silent_only: bool) -> str:
        settings = (await self.get_preferences()).settings()
        async with gmail_provider(settings, silent_only=silent_only) as provider:
            identity = await provider.connect()
            return identity.email_address

    async def disconnect(self) -> None:
        def clear() -> None:
            GmailCredentialStore().clear()

        await asyncio.to_thread(clear)

    async def ai_status(self) -> str:
        if not (await self.get_preferences()).groq_model:
            return "AI: choose a Structured Outputs model in Settings."

        def has_key() -> bool:
            return GroqKeyStore().load() is not None

        if not await asyncio.to_thread(has_key):
            return "AI: key missing. Add a Groq API key in Settings."
        drafting = ""
        if self._database is not None:
            async with self._database.session() as session:
                consent = await OwnerConsentRepository(session).get_active(
                    "groq", DRAFTING_SCOPE, DRAFTING_DISCLOSURE_VERSION
                )
            drafting = (
                " Drafting consent given."
                if consent is not None
                else " Drafting asks for consent first."
            )
        return "AI: configured. Transmission requires your approval." + drafting

    async def drafting_ready(self) -> bool:
        """Whether AI drafting can be offered: a model is chosen and a Groq key is saved."""
        if not (await self.get_preferences()).groq_model:
            return False

        def has_key() -> bool:
            return GroqKeyStore().load() is not None

        return await asyncio.to_thread(has_key)

    async def generate(
        self,
        gate: ConsentGate,
        review: ShortlistGate,
        cancel: asyncio.Event,
        progress: Callable[[SyncProgress], None],
        local_date: date | None = None,
    ) -> BriefRunResult:
        """Brief today, or ``local_date``: one of the previous seven days, checked before any
        provider is built. BriefService checks it again."""
        return await self._brief(
            gate=gate,
            review=review,
            cancel=cancel,
            progress=progress,
            local_date=local_date,
            automatic=False,
        )

    async def generate_automatic(
        self, cancel: asyncio.Event, progress: Callable[[SyncProgress], None]
    ) -> BriefRunResult:
        """One automatic run for today (ADR 0017): no review, no consent question and no past
        day. It syncs, checks threads and ranks; only the automatic-analysis permission on the
        active consent lets it send anything, and without one it reports how many messages
        are ready to review."""
        return await self._brief(
            gate=_NeverAsks(),
            review=None,
            cancel=cancel,
            progress=progress,
            local_date=None,
            automatic=True,
        )

    async def _brief(
        self,
        *,
        gate: ConsentGate,
        review: ShortlistGate | None,
        cancel: asyncio.Event,
        progress: Callable[[SyncProgress], None],
        local_date: date | None,
        automatic: bool,
    ) -> BriefRunResult:
        preferences = await self.get_owner_preferences()
        settings = await self._settings(preferences)
        zone = owner_zone(preferences)
        if local_date is not None:
            check_brief_date(local_date, datetime.now(UTC).astimezone(zone).date())
        async with (
            gmail_provider(settings, silent_only=True) as provider,
            groq_provider(settings) as ai,
            self._storage().session() as session,
        ):
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
                consent_gate=gate,
            )
            return await service.generate(
                tz_key=zone.key,
                cancel=cancel,
                progress=progress,
                shortlist_gate=review,
                shortlist_limit=preferences.shortlist_limit,
                excluded_senders=preferences.excluded_senders,
                local_date=local_date,
                automatic=automatic,
            )

    # The automatic-analysis permission (ADR 0017) lives on the active consent. Reading and
    # changing it needs no Gmail and no AI.

    async def auto_send_status(self) -> AutoSendStatus | None:
        """The permission and the disclosure it rests on; None when no account has an active
        consent yet, so there is nothing to allow."""
        async with self._storage().session() as session:
            permission = await auto_send_permission(
                session, provider=PROVIDER_NAME, version=CONSENT_DISCLOSURE_VERSION
            )
        return None if permission is None else await self._auto_send_status(permission)

    async def set_auto_send(self, limit: int) -> AutoSendStatus:
        """Allow automatic runs to send up to ``limit`` messages without asking; 0 turns it
        off. Raises ConfigurationError (a static message) without an active consent."""
        async with self._storage().session() as session:
            permission = await set_auto_send(
                session, limit, provider=PROVIDER_NAME, version=CONSENT_DISCLOSURE_VERSION
            )
        return await self._auto_send_status(permission)

    async def _auto_send_status(self, permission: AutoSendPermission) -> AutoSendStatus:
        """The permission with what one automatic run would send: the chosen model and body
        limit, from settings that can be read even when the saved preferences can't."""
        try:
            settings = await self._settings(await self.get_owner_preferences())
        except PreferencesUnavailableError:
            settings = (await self.get_preferences()).settings()
        model = (settings.groq_model or "").strip() or "the model chosen in Settings"
        preview: TransmissionPreview = permission_preview(
            max(permission.limit, 1),
            provider_name=PROVIDER_NAME,
            model_name=model,
            body_character_limit=settings.ai_body_character_limit,
            privacy_notice=PRIVACY_NOTICE,
        )
        return AutoSendStatus(**permission.model_dump(), disclosure=preview)

    # Saved briefs by day (M8 Part 3). Reading them needs no Gmail or AI.

    async def list_briefs(self) -> tuple[SavedBriefSummary, ...]:
        async with self._storage().session() as session:
            return await BriefHistory(session).list_saved()

    async def load_brief(self, account_email: str, local_date: date) -> DailyDigest | None:
        async with self._storage().session() as session:
            return await BriefHistory(session).get(account_email, local_date)

    async def missed_days(self, account_email: str) -> tuple[date, ...]:
        """The previous seven days in the owner's zone without a brief for the account.

        Listing only displays, so unreadable preferences fall back to the system zone.
        """
        try:
            zone = owner_zone(await self.get_owner_preferences())
        except PreferencesUnavailableError:
            zone = resolve_timezone(None)
        today = datetime.now(UTC).astimezone(zone).date()
        async with self._storage().session() as session:
            return await BriefHistory(session).missed_days(account_email, today)

    async def get_preferences(self) -> DesktopPreferences:
        return await asyncio.to_thread(self._preferences.load)

    async def _settings(self, preferences: OwnerPreferences) -> Settings:
        """Device settings with the saved AI limits where no MAILBRIEF_* variable is set."""
        return effective_settings((await self.get_preferences()).settings(), preferences)

    # The owner's preferences (ADR 0014) live in the database, shared with the CLI.

    async def get_owner_preferences(self) -> OwnerPreferences:
        async with self._storage().session() as session:
            return await PreferencesService(session).get()

    async def save_owner_preferences(
        self, edit: PreferencesEdit, revision: int
    ) -> OwnerPreferences:
        async with self._storage().session() as session:
            return await PreferencesService(session).save(edit, revision)

    async def reset_owner_preferences(self) -> OwnerPreferences:
        async with self._storage().session() as session:
            return await PreferencesService(session).reset()

    async def cached_accounts(self) -> tuple[CachedAccount, ...]:
        if self._database is None:
            raise RuntimeError("Desktop storage is not initialized.")
        async with self._database.session() as session:
            accounts = await AccountRepository(session).list_all()
            return tuple(
                CachedAccount(
                    account_id=account.id,
                    email_address=account.email_address,
                    last_sync_at_utc=account.last_sync_at_utc,
                )
                for account in accounts
                if account.provider == ProviderKind.GMAIL.value
            )

    async def cached_messages(self, account_id: int, day: date, offset: int = 0) -> CachedMailPage:
        try:
            zone = owner_zone(await self.get_owner_preferences())
        except PreferencesUnavailableError:
            zone = resolve_timezone(None)  # Browsing only displays local data.
        window = day_window(day, zone)
        async with self._storage().session() as session:
            account = await AccountRepository(session).get_by_id(account_id)
            if account is None or account.provider != ProviderKind.GMAIL.value:
                raise ValueError("Choose an existing cached Gmail account.")
            rows, more = await MessageRepository(session).get_cached_page(
                account_id,
                window.start_utc,
                window.end_utc,
                offset=offset,
            )
            return CachedMailPage(
                account=CachedAccount(
                    account_id=account.id,
                    email_address=account.email_address,
                    last_sync_at_utc=account.last_sync_at_utc,
                ),
                local_date=day,
                timezone_name=zone.key,
                offset=offset,
                has_more=more,
                messages=tuple(
                    MessageRepository.to_domain(
                        row,
                        account.provider_account_id,
                        ProviderKind.GMAIL,
                    )
                    for row in rows
                ),
            )

    async def save_preferences(self, preferences: DesktopPreferences) -> None:
        def save() -> None:
            if preferences.gmail_oauth_client_path is not None:
                DesktopClient.load(preferences.gmail_oauth_client_path)
            self._preferences.save(preferences)

        await asyncio.to_thread(save)

    async def save_key(self, key: SecretStr) -> None:
        def save() -> None:
            GroqKeyStore().save(key)

        await asyncio.to_thread(save)

    async def remove_key(self) -> None:
        def clear() -> None:
            GroqKeyStore().clear()

        await asyncio.to_thread(clear)

    async def revoke_consent(self) -> int:
        if self._database is None:
            raise RuntimeError("Desktop storage is not initialized.")
        async with self._database.transaction() as session:
            accounts = await AccountRepository(session).list_all()
            consents = ConsentRepository(session)
            count = 0
            now = datetime.now(UTC)
            for account in accounts:
                if account.provider == ProviderKind.GMAIL.value:
                    count += await consents.revoke_all(account.id, "groq", now)
            # AI drafting's consent belongs to the owner, not an account (ADR 0013).
            count += await OwnerConsentRepository(session).revoke_all("groq", now)
            return count

    def _storage(self) -> Database:
        if self._database is None:
            raise RuntimeError("Desktop storage is not initialized.")
        return self._database

    async def list_actions(self, view: ActionFilter) -> tuple[Action, ...]:
        async with self._storage().session() as session:
            return await ActionService(session).list_actions(view)

    async def accept_suggestion(self, suggestion_id: int) -> Action:
        async with self._storage().session() as session:
            return await ActionService(session).accept(suggestion_id)

    async def brief_links(self, digest: DailyDigest) -> dict[str, tuple[ThreadLink, ...]]:
        """The live, open actions that continue each item's thread; local data only."""
        async with self._storage().session() as session:
            return await ActionService(session).thread_links(
                digest.account_id, (item.message_key for item in digest.items)
            )

    # Follow-up proposals (ADR 0016): derived after a brief, applied only when the owner asks.

    async def brief_proposals(self, digest: DailyDigest) -> dict[str, tuple[ActionProposal, ...]]:
        """The pending proposals of live, open actions made from each item's email, by message
        key; local data only."""
        async with self._storage().session() as session:
            return await ProposalService(session).pending_for_messages(
                digest.account_id, (item.message_key for item in digest.items)
            )

    async def apply_proposal(self, proposal_id: int, revision: int) -> Action:
        async with self._storage().session() as session:
            return await ProposalService(session).apply(proposal_id, revision)

    async def undo_apply_proposal(self, proposal_id: int, revision: int) -> Action:
        async with self._storage().session() as session:
            return await ProposalService(session).undo_apply(proposal_id, revision)

    async def dismiss_proposal(self, proposal_id: int) -> None:
        async with self._storage().session() as session:
            await ProposalService(session).dismiss(proposal_id)

    async def restore_proposal(self, proposal_id: int) -> None:
        async with self._storage().session() as session:
            await ProposalService(session).restore(proposal_id)

    async def accept_into(self, suggestion_id: int, public_id: str, revision: int) -> AcceptedInto:
        async with self._storage().session() as session:
            return await ActionService(session).accept_into(suggestion_id, public_id, revision)

    async def undo_accept_into(
        self, suggestion_id: int, public_id: str, revision: int, remove_source: bool
    ) -> Action:
        async with self._storage().session() as session:
            return await ActionService(session).undo_accept_into(
                suggestion_id, public_id, revision, remove_source
            )

    async def mark_thread_seen(self, public_id: str, revision: int) -> Action:
        async with self._storage().session() as session:
            return await ActionService(session).mark_thread_seen(public_id, revision)

    async def dismiss_suggestion(self, suggestion_id: int) -> None:
        async with self._storage().session() as session:
            await ActionService(session).dismiss(suggestion_id)

    async def restore_suggestion(self, suggestion_id: int) -> None:
        async with self._storage().session() as session:
            await ActionService(session).restore_suggestion(suggestion_id)

    async def unaccept_action(self, public_id: str, revision: int) -> None:
        async with self._storage().session() as session:
            await ActionService(session).unaccept(public_id, revision)

    async def save_action(
        self,
        public_id: str,
        revision: int,
        edit: ActionEdit,
        steps: Sequence[StepEdit] | None = None,
    ) -> Action:
        async with self._storage().session() as session:
            return await ActionService(session).save(public_id, revision, edit, steps)

    async def complete_action(self, public_id: str, revision: int) -> Action:
        async with self._storage().session() as session:
            return await ActionService(session).complete(public_id, revision)

    async def reopen_action(self, public_id: str, revision: int) -> Action:
        async with self._storage().session() as session:
            return await ActionService(session).reopen(public_id, revision)

    async def delete_action(self, public_id: str, revision: int) -> None:
        async with self._storage().session() as session:
            await ActionService(session).delete(public_id, revision)

    async def restore_action(self, public_id: str) -> Action:
        async with self._storage().session() as session:
            return await ActionService(session).restore(public_id)

    async def count_actions(self, view: ActionFilter) -> int:
        async with self._storage().session() as session:
            return await ActionService(session).count_actions(view)

    # Drafts use local storage only: no call here reaches Gmail, Groq or the network.

    async def list_drafts(self) -> tuple[DraftSummary, ...]:
        async with self._storage().session() as session:
            return await DraftService(session).list_summaries()

    async def get_draft(self, public_id: str) -> Draft:
        async with self._storage().session() as session:
            return await DraftService(session).get(public_id)

    async def create_draft(self, kind: DraftKind) -> Draft:
        async with self._storage().session() as session:
            return await DraftService(session).create(kind)

    async def create_reply_draft(self, account_email: str, provider_message_id: str) -> Draft:
        async with self._storage().session() as session:
            return await DraftService(session).create_reply(account_email, provider_message_id)

    async def create_draft_for_action(self, public_id: str, kind: DraftKind) -> Draft:
        async with self._storage().session() as session:
            return await DraftService(session).create_for_action(public_id, kind)

    async def autosave_draft(self, public_id: str, revision: int, edit: DraftEdit) -> Draft:
        async with self._storage().session() as session:
            return await DraftService(session).autosave(public_id, revision, edit)

    async def checkpoint_draft(self, public_id: str, revision: int) -> DraftVersionInfo | None:
        async with self._storage().session() as session:
            return await DraftService(session).checkpoint(public_id, revision)

    async def draft_versions(self, public_id: str) -> tuple[DraftVersionInfo, ...]:
        async with self._storage().session() as session:
            return await DraftService(session).versions(public_id)

    async def draft_version(self, public_id: str, number: int) -> DraftVersion:
        async with self._storage().session() as session:
            return await DraftService(session).version(public_id, number)

    async def restore_draft_version(self, public_id: str, revision: int, number: int) -> Draft:
        async with self._storage().session() as session:
            return await DraftService(session).restore_version(public_id, revision, number)

    async def save_draft_as_new(self, public_id: str, edit: DraftEdit) -> Draft:
        async with self._storage().session() as session:
            return await DraftService(session).save_as_new(public_id, edit)

    async def delete_draft(self, public_id: str, revision: int) -> None:
        async with self._storage().session() as session:
            await DraftService(session).delete(public_id, revision)

    async def restore_draft(self, public_id: str) -> Draft:
        async with self._storage().session() as session:
            return await DraftService(session).restore(public_id)

    # AI drafting (ADR 0013). Groq and, only when the owner chooses the email, Gmail are
    # opened for each call and closed afterwards.

    async def available_drafting_parts(self, public_id: str) -> frozenset[DraftContextPart]:
        preferences = await self.get_owner_preferences()
        settings = await self._settings(preferences)
        async with groq_provider(settings) as ai, self._storage().session() as session:
            service = DraftingService(
                session,
                ai,
                BodyService(_GmailBodies(settings), limit=settings.ai_body_character_limit),
                zone=owner_zone(preferences),
                excluded_senders=preferences.excluded_senders,
            )
            return await service.available_parts(public_id)

    async def prepare_drafting(self, public_id: str, options: DraftingOptions) -> DraftingPlan:
        """Build what would be sent; choosing the email downloads its body now, in memory."""
        preferences = await self.get_owner_preferences()
        settings = await self._settings(preferences)
        async with groq_provider(settings) as ai, self._storage().session() as session:
            service = DraftingService(
                session,
                ai,
                BodyService(_GmailBodies(settings), limit=settings.ai_body_character_limit),
                zone=owner_zone(preferences),
                excluded_senders=preferences.excluded_senders,
            )
            return await service.prepare(public_id, options)

    async def generate_draft(
        self, plan: DraftingPlan, gate: DraftingGate, cancel: asyncio.Event
    ) -> DraftingOutcome:
        preferences = await self.get_owner_preferences()
        settings = await self._settings(preferences)
        async with groq_provider(settings) as ai, self._storage().session() as session:
            service = DraftingService(
                session,
                ai,
                zone=owner_zone(preferences),
                excluded_senders=preferences.excluded_senders,
            )
            return await service.generate(plan, gate, cancel)

    async def close(self) -> None:
        if self._database is not None:
            await self._database.dispose()
            self._database = None
