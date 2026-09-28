"""Desktop composition; every operation owns and closes its provider/session resources."""

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path

from pydantic import SecretStr

from mailbrief.domain.actions import Action, ActionEdit, ActionFilter, StepEdit
from mailbrief.domain.briefs import BriefRunResult
from mailbrief.domain.cached_mail import CachedAccount, CachedMailPage
from mailbrief.domain.digests import DailyDigest, SyncProgress
from mailbrief.domain.messages import ProviderKind
from mailbrief.providers.gmail.cache import GmailCredentialStore
from mailbrief.providers.gmail.factory import gmail_provider
from mailbrief.providers.gmail.oauth import DesktopClient
from mailbrief.providers.groq.credentials import GroqKeyStore
from mailbrief.providers.groq.factory import groq_provider
from mailbrief.services.actions import ActionService
from mailbrief.services.analysis import AnalysisService
from mailbrief.services.application import ApplicationService
from mailbrief.services.bodies import BodyService
from mailbrief.services.brief import BriefService, ConsentGate, ShortlistGate
from mailbrief.services.calendar import local_day_window, resolve_timezone
from mailbrief.services.digest import DigestService
from mailbrief.storage.database import Database
from mailbrief.storage.migrate import upgrade_database
from mailbrief.storage.repositories import (
    AccountRepository,
    ConsentRepository,
    DigestRepository,
    MessageRepository,
    SyncRunRepository,
)
from mailbrief.ui.preferences import DesktopPreferences, PreferencesStore


class DesktopRuntime:
    """Use the same settings, vault, migrations and services as the diagnostic CLI."""

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
        return "AI: configured. Transmission requires your approval."

    async def generate(
        self,
        gate: ConsentGate,
        review: ShortlistGate,
        cancel: asyncio.Event,
        progress: Callable[[SyncProgress], None],
    ) -> BriefRunResult:
        if self._database is None:
            raise RuntimeError("Desktop storage is not initialized.")
        settings = (await self.get_preferences()).settings()
        async with (
            gmail_provider(settings, silent_only=True) as provider,
            groq_provider(settings) as ai,
            self._database.session() as session,
        ):
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
                consent_gate=gate,
            )
            return await service.generate(cancel=cancel, progress=progress, shortlist_gate=review)

    async def get_preferences(self) -> DesktopPreferences:
        return await asyncio.to_thread(self._preferences.load)

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
        if self._database is None:
            raise RuntimeError("Desktop storage is not initialized.")
        zone = resolve_timezone(None)
        window = local_day_window(datetime.combine(day, time(12), tzinfo=zone), zone)
        async with self._database.session() as session:
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
            for account in accounts:
                if account.provider == ProviderKind.GMAIL.value:
                    count += await consents.revoke_all(account.id, "groq", datetime.now(UTC))
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

    async def close(self) -> None:
        if self._database is not None:
            await self._database.dispose()
            self._database = None
