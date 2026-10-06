"""Opt-in package smoke check with no real account, vault, network or profile access."""

import asyncio
import importlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from PySide6.QtCore import QLibraryInfo
from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics, QGuiApplication
from PySide6.QtWidgets import QApplication

from mailbrief.domain.drafts import DraftEdit, DraftKind
from mailbrief.domain.messages import AccountIdentity, EmailContact, NormalizedMessage, ProviderKind
from mailbrief.domain.preferences import PreferencesEdit
from mailbrief.services.data import (
    CleanupKind,
    CleanupRequest,
    apply_cleanup,
    export_writing,
    preview_cleanup,
)
from mailbrief.storage.backup import create_backup
from mailbrief.storage.database import Database
from mailbrief.storage.recovery import inspect_backup, restore_backup
from mailbrief.storage.repositories import AccountRepository, MessageRepository
from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.preferences import DesktopPreferences
from mailbrief.ui.preferences_view import region_zones
from mailbrief.ui.runtime import DesktopRuntime
from mailbrief.ui.theme import FONT_FAMILY, current_tokens, icon_pixmap

SMOKE_ZONE = "America/New_York"


async def seed_metadata(path: Path) -> None:
    """Populate the explicit disposable database with a synthetic cached message."""
    database = await asyncio.to_thread(Database.from_path, path)
    try:
        async with database.transaction() as session:
            account = await AccountRepository(session).upsert(
                AccountIdentity(
                    provider=ProviderKind.GMAIL,
                    provider_account_id="package-smoke",
                    email_address="smoke@example.invalid",
                )
            )
            await MessageRepository(session).upsert_messages(
                account.id,
                [
                    NormalizedMessage(
                        provider=ProviderKind.GMAIL,
                        provider_account_id="package-smoke",
                        provider_message_id="synthetic-message",
                        subject="Package check",
                        sender=EmailContact(address="sender@example.invalid"),
                        received_at_utc=datetime.now(UTC),
                        is_read=False,
                        has_attachments=False,
                        body_preview="Synthetic metadata preview.",
                        web_link="https://mail.google.com/mail/u/?authuser=smoke%40example.invalid#all/test",
                    )
                ],
            )
    finally:
        await database.dispose()


async def check_package(directory: Path) -> None:
    """Exercise two launches, settings and preferences, bundled migrations, Qt, TLS and
    time zone data."""
    # Class priority checks dependencies without constructing a vault or reading secrets.
    backend = "Windows" if sys.platform == "win32" else "macOS"
    module = importlib.import_module(f"keyring.backends.{backend}")
    backend_class = module.WinVaultKeyring if sys.platform == "win32" else module.Keyring
    if backend_class.priority <= 0:
        raise RuntimeError("Native vault backend unavailable.")
    plugin = "qwindows.dll" if sys.platform == "win32" else "libqcocoa.dylib"
    plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    if not (plugins / "platforms" / plugin).is_file():
        raise RuntimeError("Native Qt platform plugin unavailable.")
    application = QApplication.instance()
    if not isinstance(application, QApplication):
        raise RuntimeError("Qt application unavailable.")
    if sys.platform == "win32" and QGuiApplication.platformName() == "offscreen":
        # The Windows offscreen plugin has no system font discovery. Use an OS font
        # for the rendering check without bundling it or touching a user profile.
        font_path = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "Fonts" / "segoeui.ttf"
        identifier = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(identifier)
        if not families:
            raise RuntimeError("Smoke font unavailable.")
        application.setFont(QFont(families[0], 9))
    if not QFontMetrics(application.font()).inFontUcs4(ord("A")):
        raise RuntimeError("Qt text rendering unavailable.")
    # Creating the HTTP client loads the bundled TLS trust data without sending a request.
    async with httpx.AsyncClient(trust_env=False):
        pass
    ZoneInfo("America/Toronto")
    if SMOKE_ZONE not in region_zones():
        raise RuntimeError("Time zone list unavailable.")
    if FONT_FAMILY not in QFontDatabase.families():
        raise RuntimeError("Bundled font unavailable.")
    if icon_pixmap("refresh", current_tokens().text, 16).isNull():
        raise RuntimeError("Bundled icons unavailable.")
    for _ in range(2):
        runtime = DesktopRuntime(directory / "smoke.sqlite3")
        try:
            await runtime.load_saved()
            await seed_metadata(directory / "smoke.sqlite3")
            await runtime.save_preferences(DesktopPreferences(groq_model="smoke-test-model"))
            if (await runtime.get_preferences()).groq_model != "smoke-test-model":
                raise RuntimeError("Settings restoration failed.")
            owner = await runtime.get_owner_preferences()
            if owner.revision == 0:
                owner = await runtime.save_owner_preferences(
                    PreferencesEdit(time_zone=SMOKE_ZONE), 0
                )
            if owner.time_zone != SMOKE_ZONE:
                raise RuntimeError("Preferences restoration failed.")
            if not await runtime.list_drafts():
                note = await runtime.create_draft(DraftKind.NOTE)
                note = await runtime.autosave_draft(
                    note.public_id,
                    note.revision,
                    DraftEdit(title="Package note", body="Synthetic owner writing."),
                )
                await runtime.checkpoint_draft(note.public_id, note.revision)
            note_summary = (await runtime.list_drafts())[0]
            note = await runtime.get_draft(note_summary.public_id)
            if note.body != "Synthetic owner writing." or not await runtime.draft_versions(
                note.public_id
            ):
                raise RuntimeError("Writing or version restoration failed.")
            window = MainWindow(runtime, database_path=directory / "smoke.sqlite3")
            window.show()
            QApplication.processEvents()
            if window.grab().isNull():
                raise RuntimeError("Qt rendering failed.")
            await window._open_data()
            if window.data_dialog is None or window.data_dialog.grab().isNull():
                raise RuntimeError("Recovery controls failed to render.")
            window.data_dialog.reject()
            accounts = await runtime.cached_accounts()
            window.cached_dialog.configure(accounts)
            # The saved zone decides the day, whatever the system zone is.
            page = await runtime.cached_messages(
                accounts[0].account_id, datetime.now(UTC).astimezone(ZoneInfo(SMOKE_ZONE)).date()
            )
            if len(page.messages) != 1:
                raise RuntimeError("Offline metadata restoration failed.")
            window.cached_dialog.show_page(page)
            window.cached_dialog.open()
            QApplication.processEvents()
            if "Synthetic metadata preview." not in window.cached_dialog.details.toPlainText():
                raise RuntimeError("Cached metadata display failed.")
            window.cached_dialog.reject()
            window.close()
            await window.shutdown()
        finally:
            await runtime.close()
    suffix = uuid4().hex
    archive = directory / f"smoke-backup-{suffix}.zip"
    export = directory / f"smoke-writing-{suffix}.json"
    restored = directory / f"smoke-restored-{suffix}.sqlite3"
    await asyncio.to_thread(create_backup, directory / "smoke.sqlite3", archive)
    await asyncio.to_thread(inspect_backup, archive)
    await asyncio.to_thread(restore_backup, archive, restored)
    database = Database.from_path(restored)
    try:
        await export_writing(database, export)
        payload = json.loads(await asyncio.to_thread(export.read_text, encoding="utf-8"))
        if payload["records"]["drafts"][0]["body"] != "Synthetic owner writing.":
            raise RuntimeError("Portable writing export failed.")
        preview = await preview_cleanup(database, CleanupRequest(CleanupKind.CACHE, 1))
        await apply_cleanup(database, preview)
    finally:
        await database.dispose()
    recovered = DesktopRuntime(restored)
    try:
        await recovered.load_saved()
        if len(await recovered.list_drafts()) != 1:
            raise RuntimeError("Cache cleanup removed owner writing.")
    finally:
        await recovered.close()
    report = json.dumps({"ok": True, "platform": sys.platform, "launches": 2})
    await asyncio.to_thread((directory / "smoke-result.json").write_text, report, encoding="utf-8")
