"""Keyboard accessible offline data controls; destructive changes always need a preview."""

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from mailbrief import __version__
from mailbrief.domain.backup import BackupMetadata
from mailbrief.domain.cached_mail import CachedAccount
from mailbrief.infra.files import write_text_atomically
from mailbrief.services.data import (
    CleanupChangedError,
    CleanupKind,
    CleanupRequest,
    apply_cleanup,
    export_writing,
    preview_cleanup,
)
from mailbrief.storage.backup import create_backup
from mailbrief.storage.database import Database
from mailbrief.storage.recovery import BackupValidationError, inspect_backup
from mailbrief.ui.deadline_text import moment_text
from mailbrief.ui.diagnostics import log_failure

HELP = """Your data stays on this computer. Disconnect removes Gmail credentials only.
Backup saves a consistent database snapshot, including your writing and preferences;
OAuth tokens and API keys stay in the OS credential vault. Backups are not encrypted.
Export saves all actions, drafts and notes, including deleted writing, source snapshots
and saved versions, as portable UTF-8 JSON. Keep backups and exports private.
Restore validates and upgrades the archive, closes MailBrief, keeps a pre-restore copy
beside the database, and clears automatic AI permission. Relaunch after restoring.
Device setup (OAuth client path and model) and vault credentials are configured separately.

Cleanup is manual. Cache cleanup removes selected saved mail, analyses, suggestions,
briefs and sync records. Source snapshots, writing and remembered decisions survive.
Removing an account also removes its saved AI consent; credentials are unchanged.
Permanently removing deleted writing removes its steps, sources, proposals and versions;
live and completed writing survives. Restore from a backup to recover permanent deletions.
MailBrief never sends mail or writes to Gmail.

If saving fails: stop editing, check free disk space and folder access, then retry.
If an upgrade fails: keep the original and its pre-upgrade copy, fix storage access and retry.
If restore fails: the current database is retained. Close other database clients and retry.
Never manually remove SQLite -wal/-shm/-journal files. For a damaged database, use the
offline restore command with a new filename and retain the damaged database and sidecars.
Only restore archives you intend to use; a checksum verifies integrity, not origin.

Setup: choose a Google Desktop OAuth client JSON in Settings, connect read-only Gmail,
then choose a Structured Outputs model and save your Groq key if you want AI features.
Enable Groq Zero Data Retention in its console before approving real mail transmission.
Use Tab/Shift+Tab to navigate, Alt+underlined letter for buttons, and Escape to close.
"""

_INSIDE_DATA = "Choose a destination outside MailBrief's data folder."


class DestinationInsideDataError(ValueError):
    """Backups and exports must be stored outside MailBrief's data folder."""

    def __init__(self) -> None:
        super().__init__(_INSIDE_DATA)


class DataDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        database_path: Path,
        run: Callable[[Callable[[], Awaitable[None]]], object],
        request_restore: Callable[[Path, BackupMetadata], None],
        refresh: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(parent)
        self.path = database_path
        self.run = run
        self.request_restore = request_restore
        self.refresh = refresh
        # The owner's zone, for the backup's time; the window sets it, as for cached mail.
        self.zone = ZoneInfo("UTC")
        # The owner's day, to name the year of an older backup; the window sets it.
        self.today: Callable[[], date] = lambda: datetime.now(self.zone).date()
        self._picker: QFileDialog | None = None
        self._confirmation: QMessageBox | None = None
        self._storage_available = False
        self.setWindowTitle("MailBrief — Data and recovery")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(640, 650)
        layout = QVBoxLayout(self)
        content = QWidget()
        controls = QVBoxLayout(content)
        hint = QLabel(
            "Back up or export your writing before cleanup. Backups and writing exports "
            "are private and unencrypted. Credentials stay in the OS vault. "
            "See Help and setup for recovery instructions."
        )
        hint.setTextFormat(Qt.TextFormat.PlainText)
        hint.setWordWrap(True)
        controls.addWidget(hint)
        self.buttons: list[QPushButton] = []
        for title, operation in (
            ("Create &backup…", self.backup),
            ("&Export all writing…", self.export),
            ("&Verify backup…", self.verify),
            ("&Restore backup and exit…", self.restore),
            ("Export safe &diagnostics…", self.diagnostics),
            ("&Preview cleanup…", self.cleanup),
        ):
            button = QPushButton(title)
            button.clicked.connect(lambda _checked=False, op=operation: self.run(op))
            controls.addWidget(button)
            self.buttons.append(button)
        self.account = QComboBox()
        self.account.setPlaceholderText("No saved Gmail account")
        self.account.setAccessibleName("Saved Gmail account for cleanup")
        controls.addWidget(self.account)
        self.kind = QComboBox()
        self.kind.setAccessibleName("Cleanup operation")
        self.kind.addItem("Cached mail older than the selected age", "old-cache")
        self.kind.addItem("All cached mail for this account", "all-cache")
        self.kind.addItem("Remove saved account data", "account")
        self.kind.addItem(
            "Permanently remove deleted writing older than the selected age", "deleted"
        )
        self.kind.addItem("Forget old decisions with no saved account or action", "decisions")
        controls.addWidget(self.kind)
        self.days = QSpinBox()
        self.days.setRange(1, 36500)
        self.days.setValue(90)
        self.days.setSuffix(" days")
        self.days.setAccessibleName("Minimum age for cleanup")
        controls.addWidget(self.days)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        tabs = QTabWidget()
        tabs.addTab(scroll, "&Data")
        help_view = QTextBrowser()
        help_view.setAccessibleName("Setup and data recovery instructions")
        help_view.setPlainText(HELP)
        tabs.addTab(help_view, "&Help and setup")
        layout.addWidget(tabs)
        self.status = QLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        close = QPushButton("&Close")
        close.clicked.connect(self.reject)
        layout.addWidget(close)

    def set_busy(self, busy: bool) -> None:
        for control in (*self.buttons, self.account, self.kind, self.days):
            control.setEnabled(not busy)
        self.buttons[-1].setEnabled(not busy and self._storage_available)

    def configure(self, accounts: tuple[CachedAccount, ...], *, available: bool) -> None:
        self._storage_available = available
        self.account.clear()
        for account in accounts:
            self.account.addItem(account.email_address, account.account_id)
        if accounts:
            self.account.setCurrentIndex(0)
        if not available:
            self.status.setText(
                "Saved data is unavailable. You can verify a backup or attempt recovery."
            )

    async def choose(self, title: str, pattern: str, *, save: bool) -> Path | None:
        if not self.isVisible():
            return None
        picker = QFileDialog(self, title)
        self._picker = picker
        picker.setAcceptMode(
            QFileDialog.AcceptMode.AcceptSave if save else QFileDialog.AcceptMode.AcceptOpen
        )
        picker.setFileMode(
            QFileDialog.FileMode.AnyFile if save else QFileDialog.FileMode.ExistingFile
        )
        picker.setNameFilter(pattern)
        if save:
            picker.setDefaultSuffix("zip" if "*.zip" in pattern else "json")
        picker.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        picker.finished.connect(
            lambda result: future.set_result(result) if not future.done() else None
        )
        picker.open()
        try:
            if await future != QDialog.DialogCode.Accepted or not picker.selectedFiles():
                return None
            path = Path(picker.selectedFiles()[0])
            inside = await asyncio.to_thread(
                lambda: path.resolve().is_relative_to(self.path.parent.resolve())
            )
            if save and inside:
                raise DestinationInsideDataError()
            return path
        finally:
            picker.reject()
            picker.deleteLater()
            self._picker = None

    async def confirm(self, message: str) -> bool:
        if not self.isVisible():
            return False
        box = QMessageBox(self)
        self._confirmation = box
        box.setWindowTitle("Review data change")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        box.finished.connect(
            lambda result: future.set_result(result) if not future.done() else None
        )
        box.open()
        try:
            return await future == QMessageBox.StandardButton.Ok
        finally:
            box.reject()
            box.deleteLater()
            self._confirmation = None

    async def backup(self) -> None:
        path = await self.choose("Create a new private backup", "ZIP archive (*.zip)", save=True)
        if path is not None:
            await asyncio.to_thread(create_backup, self.path, path)
            self.status.setText("Backup saved. Keep it private; it is not encrypted.")

    async def export(self) -> None:
        path = await self.choose("Export all owner writing", "JSON (*.json)", save=True)
        if path is not None:
            database = Database.from_path(self.path)
            try:
                await export_writing(database, path)
            finally:
                await database.dispose()
            self.status.setText("Writing exported, including deleted records and saved versions.")

    async def verify(self) -> None:
        path = await self.choose("Verify a backup", "ZIP archive (*.zip)", save=False)
        if path is not None:
            metadata = await asyncio.to_thread(inspect_backup, path)
            self.status.setText(f"Valid backup from {self._backup_time(metadata)}.")

    def _backup_time(self, metadata: BackupMetadata) -> str:
        """When the backup was made, in the owner's zone, with the year if it isn't this one."""
        return moment_text(metadata.created_at_utc, self.zone, self.today())

    async def restore(self) -> None:
        path = await self.choose("Restore a backup", "ZIP archive (*.zip)", save=False)
        if path is None:
            return
        metadata = await asyncio.to_thread(inspect_backup, path)
        if await self.confirm(
            f"Replace saved data with the backup from {self._backup_time(metadata)}? "
            "MailBrief will exit, preserve the current database beside it, and clear automatic "
            "AI permission. Credentials stay in the vault. Relaunch after the result appears."
        ):
            self.request_restore(path, metadata)

    async def diagnostics(self) -> None:
        path = await self.choose("Export safe diagnostics", "JSON (*.json)", save=True)
        if path is not None:
            payload = (
                json.dumps(
                    {
                        "format": "mailbrief-diagnostics",
                        "version": 1,
                        "app_version": __version__,
                        "platform": sys.platform,
                        "python": list(sys.version_info[:3]),
                    },
                    indent=2,
                )
                + "\n"
            )
            await asyncio.to_thread(write_text_atomically, path, payload, overwrite=False)
            self.status.setText(
                "Safe diagnostics saved: application, platform and Python versions."
            )

    async def cleanup(self) -> None:
        choice = self.kind.currentData()
        cutoff = datetime.now(UTC) - timedelta(days=self.days.value())
        request = CleanupRequest(
            {
                "old-cache": CleanupKind.CACHE,
                "all-cache": CleanupKind.CACHE,
                "account": CleanupKind.ACCOUNT,
                "deleted": CleanupKind.DELETED,
                "decisions": CleanupKind.ORPHAN_DECISIONS,
            }[choice],
            None if choice in ("deleted", "decisions") else self.account.currentData(),
            cutoff if choice in ("deleted", "decisions", "old-cache") else None,
        )
        database = Database.from_path(self.path)
        try:
            preview = await preview_cleanup(database, request)
            detail = " Live writing and remembered decisions survive. Credentials are unchanged."
            if request.kind == CleanupKind.DELETED:
                detail += " Selected writing cannot be undone without a backup."
            elif request.kind == CleanupKind.ORPHAN_DECISIONS:
                detail = (
                    " Writing survives. Forgotten suggestions may appear again after reconnecting."
                )
            else:
                detail += " Cached analyses/suggestions and brief membership are also removed."
            if await self.confirm(preview.summary + detail):
                await apply_cleanup(database, preview)
                try:
                    await self.refresh()
                except Exception as exc:
                    log_failure(exc)
                    self.status.setText(
                        "Cleanup completed. Views could not refresh; restart MailBrief."
                    )
                else:
                    self.status.setText("Reviewed cleanup completed.")
        finally:
            await database.dispose()

    async def guarded(self, operation: Callable[[], Awaitable[None]]) -> None:
        try:
            await operation()
        except FileExistsError:
            self.status.setText("That destination already exists. Choose a new filename.")
        except BackupValidationError:
            self.status.setText("This backup is damaged or incompatible. Saved data is unchanged.")
        except CleanupChangedError:
            self.status.setText("Saved data changed. Review a new cleanup preview.")
        except DestinationInsideDataError:
            self.status.setText(_INSIDE_DATA)
        except Exception as exc:
            log_failure(exc)
            self.status.setText(
                "Data operation could not finish. Check destination access, free disk space "
                "and backup compatibility. Existing files are kept; review a new cleanup "
                "preview if saved data changed."
            )

    def reject(self) -> None:
        if self._picker is not None:
            self._picker.reject()
        if self._confirmation is not None:
            self._confirmation.reject()
        super().reject()
