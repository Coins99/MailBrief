"""Opt-in package smoke check with no real account, vault, network or profile access."""

import asyncio
import importlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from PySide6.QtWidgets import QApplication

from mailbrief.domain.messages import AccountIdentity, EmailContact, NormalizedMessage, ProviderKind
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, MessageRepository
from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.preferences import DesktopPreferences
from mailbrief.ui.runtime import DesktopRuntime


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
    """Exercise two launches, settings, bundled migrations, Qt, TLS and timezone data."""
    # Constructing a vault backend could access real credentials; import only.
    backend = "Windows" if sys.platform == "win32" else "macOS"
    importlib.import_module(f"keyring.backends.{backend}")
    # Creating the HTTP client loads the bundled TLS trust data without sending a request.
    async with httpx.AsyncClient(trust_env=False):
        pass
    ZoneInfo("America/Toronto")
    for _ in range(2):
        runtime = DesktopRuntime(directory / "smoke.sqlite3")
        try:
            await runtime.load_saved()
            await seed_metadata(directory / "smoke.sqlite3")
            await runtime.save_preferences(DesktopPreferences(groq_model="smoke-test-model"))
            if (await runtime.get_preferences()).groq_model != "smoke-test-model":
                raise RuntimeError("Settings restoration failed.")
            window = MainWindow(runtime)
            window.show()
            QApplication.processEvents()
            if window.grab().isNull():
                raise RuntimeError("Qt rendering failed.")
            accounts = await runtime.cached_accounts()
            window.cached_dialog.configure(accounts)
            page = await runtime.cached_messages(
                accounts[0].account_id, datetime.now(UTC).astimezone().date()
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
    report = json.dumps({"ok": True, "platform": sys.platform, "launches": 2})
    await asyncio.to_thread((directory / "smoke-result.json").write_text, report, encoding="utf-8")
