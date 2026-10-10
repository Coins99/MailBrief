"""Desktop production composition with temporary storage and fake providers."""

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from PySide6.QtCore import QLibraryInfo, QLocale
from pytestqt.qtbot import QtBot

from mailbrief.config import Settings
from mailbrief.domain.digests import DigestStatus
from mailbrief.domain.messages import AccountIdentity, ProviderKind
from mailbrief.services.brief import BriefService
from mailbrief.storage.database import Database
from mailbrief.storage.repositories import AccountRepository, DigestRepository
from mailbrief.ui import runtime
from mailbrief.ui.main_window import MainWindow
from mailbrief.ui.runtime import DesktopRuntime
from tests.ui.test_workflow import FakeBackend
from tests.ui.window_wait import WindowWait
from tests.unit.services.ai_fakes import FakeAIProvider
from tests.unit.services.test_sync import FakeEmailProvider


async def test_saved_brief_restores_across_launches_without_provider(tmp_path: Path) -> None:
    path = tmp_path / "mailbrief.sqlite3"
    first = DesktopRuntime(path)
    assert await first.load_saved() is None
    database = Database.from_path(path)
    try:
        async with database.transaction() as session:
            accounts = AccountRepository(session)
            digests = DigestRepository(session)
            for kind in (ProviderKind.GMAIL, ProviderKind.MICROSOFT):
                account = await accounts.upsert(
                    AccountIdentity(
                        provider=kind,
                        provider_account_id=kind.value,
                        email_address=f"{kind.value}@example.com",
                        display_name="Test",
                    )
                )
                await digests.save_digest(
                    account_id=account.id,
                    local_date=date(2026, 9, 4),
                    timezone_name="UTC",
                    status=DigestStatus.EMPTY,
                    items=[],
                )
    finally:
        await database.dispose()
        await first.close()
    second = DesktopRuntime(path)
    try:
        saved = await second.load_saved()
        assert saved is not None
        assert saved.account_id == "gmail@example.com"
        assert saved.status is DigestStatus.EMPTY
    finally:
        await second.close()
        await second.close()


async def test_generation_cancellation_closes_both_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    closed: list[str] = []

    @asynccontextmanager
    async def email(settings: Settings, *, silent_only: bool) -> AsyncIterator[FakeEmailProvider]:
        assert silent_only
        try:
            yield FakeEmailProvider(pages=[])
        finally:
            closed.append("gmail")

    @asynccontextmanager
    async def ai(settings: Settings) -> AsyncIterator[FakeAIProvider]:
        try:
            yield FakeAIProvider()
        finally:
            closed.append("groq")

    monkeypatch.setattr(runtime, "gmail_provider", email)
    monkeypatch.setattr(runtime, "groq_provider", ai)
    monkeypatch.setattr(BriefService, "generate", AsyncMock(side_effect=asyncio.CancelledError))
    backend = DesktopRuntime(tmp_path / "mailbrief.sqlite3")
    await backend.load_saved()
    gate = MainWindow(FakeBackend())
    qtbot.addWidget(gate)
    try:
        with pytest.raises(asyncio.CancelledError):
            await backend.generate(gate, gate, asyncio.Event(), lambda progress: None)
        assert closed == ["groq", "gmail"]
    finally:
        await backend.close()


@pytest.mark.parametrize("quit_via_qt", [False, True])
@pytest.mark.parametrize("during_run", [False, True])
def test_real_qasync_loop_closes_window_and_backend(
    qtbot: QtBot,
    themed: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    quit_via_qt: bool,
    during_run: bool,
) -> None:
    from PySide6.QtWidgets import QApplication

    from mailbrief import app
    from mailbrief.paths import AppPaths

    class SlowClosingBackend(FakeBackend):
        async def close(self) -> None:
            database = Database.from_path(tmp_path / "cleanup.sqlite3")
            async with database.session() as session:
                from sqlalchemy import text

                await session.execute(text("SELECT 1"))
            await asyncio.sleep(0.01)
            await database.dispose()
            await super().close()

    backend = SlowClosingBackend()
    monkeypatch.setattr(app, "DesktopRuntime", lambda path: backend)
    monkeypatch.setattr(
        AppPaths,
        "from_qt",
        lambda: AppPaths(
            data_dir=tmp_path,
            database_path=tmp_path / "unused.sqlite3",
            microsoft_token_cache_path=tmp_path / "unused.bin",
        ),
    )

    def close_window(window: MainWindow) -> bool:
        if during_run and not window.review_panel.isVisible():
            if window.generate_button.isEnabled():
                window.generate_button.click()
            return False  # Try again once the review is showing.
        if quit_via_qt:
            QApplication.quit()
        else:
            window.close()
        return True

    # Applying the theme can delay the first show; keep looking until it appears.
    wait = WindowWait(close_window)
    wait.start()
    assert wait.run(lambda: app.main([])) == 0
    assert backend.closed
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    application.setQuitOnLastWindowClosed(True)
    asyncio.set_event_loop(None)


@pytest.mark.parametrize("light", [True, False])
def test_a_theme_failure_never_stops_the_desktop(
    qtbot: QtBot,
    themed: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    light: bool,
) -> None:
    """Unthemed, the painted widgets take the tokens that suit the palette in effect, and
    desktop.log records the failure."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication

    from mailbrief import app
    from mailbrief.paths import AppPaths
    from mailbrief.ui.theme import DARK, LIGHT, current_tokens

    def broken_stylesheet(*args: object) -> str:
        raise RuntimeError("Theme stylesheet failed.")

    qt = QApplication.instance()
    assert isinstance(qt, QApplication)
    palette = qt.palette()  # The platform's own, as the theme would have replaced it.
    window_colour = Qt.GlobalColor.white if light else Qt.GlobalColor.black
    palette.setColor(QPalette.ColorRole.Window, QColor(window_colour))
    qt.setPalette(palette)
    backend = FakeBackend()
    # A real step of the real apply_theme fails, so the tokens below prove its rollback.
    monkeypatch.setattr("mailbrief.ui.theme.style.build_stylesheet", broken_stylesheet)
    monkeypatch.setattr(app, "DesktopRuntime", lambda path: backend)
    monkeypatch.setattr(
        AppPaths,
        "from_qt",
        lambda: AppPaths(
            data_dir=tmp_path,
            database_path=tmp_path / "unused.sqlite3",
            microsoft_token_cache_path=tmp_path / "unused.bin",
        ),
    )

    def close(window: MainWindow) -> bool:
        window.close()
        return True

    wait = WindowWait(close)
    wait.start()
    try:
        assert wait.run(lambda: app.main([])) == 0
        assert backend.closed
        assert current_tokens() is (LIGHT if light else DARK)
        assert "exception=RuntimeError" in (tmp_path / "desktop.log").read_text("utf-8")
    finally:
        application = QApplication.instance()
        assert isinstance(application, QApplication)
        application.setQuitOnLastWindowClosed(True)
        asyncio.set_event_loop(None)


def test_offline_package_mode_never_opens_profile_or_vault(
    qtbot: QtBot,
    themed: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from PySide6.QtWidgets import QApplication

    from mailbrief import app
    from mailbrief.infra import vault
    from mailbrief.paths import AppPaths

    forbidden = Mock(side_effect=AssertionError("No real profile or vault in a smoke check"))
    monkeypatch.setattr(AppPaths, "from_qt", forbidden)
    monkeypatch.setattr(vault, "os_vault", forbidden)
    try:
        assert app.main(["--smoke-test-dir", str(tmp_path)]) == 0
        report = json.loads((tmp_path / "smoke-result.json").read_text())
        assert report["ok"] is True
        assert report["launches"] == 2
        forbidden.assert_not_called()
    finally:
        application = QApplication.instance()
        assert isinstance(application, QApplication)
        application.setQuitOnLastWindowClosed(True)
        asyncio.set_event_loop(None)


def test_package_failure_reports_type_without_sensitive_exception(
    qtbot: QtBot,
    themed: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from PySide6.QtWidgets import QApplication

    from mailbrief import app
    from mailbrief.ui import smoke

    monkeypatch.setattr(
        smoke, "check_package", AsyncMock(side_effect=RuntimeError("SECRET_MARKER"))
    )
    try:
        assert app.main(["--smoke-test-dir", str(tmp_path)]) == 1
        report = (tmp_path / "smoke-result.json").read_text()
        assert "RuntimeError" in report
        assert "SECRET_MARKER" not in report
    finally:
        application = QApplication.instance()
        assert isinstance(application, QApplication)
        application.setQuitOnLastWindowClosed(True)
        asyncio.set_event_loop(None)


def test_main_switches_to_english_before_any_widget(
    qtbot: QtBot,
    themed: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from PySide6.QtWidgets import QApplication

    from mailbrief import app
    from mailbrief.ui import smoke

    seen: list[QLocale.Language] = []

    async def check(_directory: Path) -> None:  # The first code to make widgets.
        seen.append(QLocale().language())

    monkeypatch.setattr(smoke, "check_package", check)
    # A French system language; ``themed`` puts the previous default back.
    QLocale.setDefault(QLocale(QLocale.Language.French, QLocale.Country.France))
    try:
        assert app.main(["--smoke-test-dir", str(tmp_path)]) == 0
        assert seen == [QLocale.Language.English]
    finally:
        application = QApplication.instance()
        assert isinstance(application, QApplication)
        application.setQuitOnLastWindowClosed(True)
        asyncio.set_event_loop(None)


async def test_second_instance_does_not_open_backend(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QMessageBox

    from mailbrief import app
    from mailbrief.paths import AppPaths

    monkeypatch.setattr(
        AppPaths,
        "from_qt",
        lambda: AppPaths(
            data_dir=tmp_path,
            database_path=tmp_path / "test.sqlite3",
            microsoft_token_cache_path=tmp_path / "unused.bin",
        ),
    )
    backend = Mock(return_value=FakeBackend())
    monkeypatch.setattr(app, "DesktopRuntime", backend)
    notice = Mock()
    monkeypatch.setattr(QMessageBox, "information", notice)
    lock = QLockFile(str(tmp_path / "desktop.lock"))
    assert lock.tryLock(0)
    try:
        await app.run_desktop()
        backend.assert_not_called()
        assert "already running" in notice.call_args.args[2]
    finally:
        lock.unlock()


@pytest.mark.parametrize("missing", ["plugin", "vault"])
async def test_smoke_rejects_missing_native_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    from mailbrief.ui import smoke

    if missing == "plugin":
        monkeypatch.setattr(QLibraryInfo, "path", lambda _: str(tmp_path))
    else:
        from unittest.mock import PropertyMock

        backend = Mock()
        type(backend).priority = PropertyMock(side_effect=RuntimeError("Unavailable"))
        monkeypatch.setattr(
            importlib,
            "import_module",
            lambda _: Mock(WinVaultKeyring=backend, Keyring=backend),
        )
    with pytest.raises(RuntimeError):
        await smoke.check_package(tmp_path)
    assert not (tmp_path / "smoke-result.json").exists()
