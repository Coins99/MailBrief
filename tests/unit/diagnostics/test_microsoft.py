"""Tests for the Microsoft live-retrieval diagnostic."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mailbrief.config import Settings
from mailbrief.diagnostics.microsoft import _run_fetch, main
from mailbrief.domain.messages import AccountIdentity, MessagePage, ProviderKind
from mailbrief.errors import ConfigurationError
from mailbrief.paths import AppPaths
from mailbrief.ports.errors import (
    AuthenticationCancelledError,
    AuthenticationRequiredError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from mailbrief.providers.microsoft import MicrosoftEmailProvider


def _paths(root: Path) -> AppPaths:
    return AppPaths(root, root / "mailbrief.sqlite3", root / "tokens.bin")


def _account() -> AccountIdentity:
    return AccountIdentity(
        provider=ProviderKind.MICROSOFT,
        provider_account_id="account",
        email_address="user@example.com",
    )


async def test_fetch_prints_only_account_and_first_page_count(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = MagicMock(spec=MicrosoftEmailProvider)
    provider.connect = AsyncMock(return_value=_account())
    closed = False

    async def pages() -> AsyncIterator[MessagePage]:
        nonlocal closed
        try:
            yield MessagePage(page_number=1, messages=())
            pytest.fail("Diagnostic requested more than one page")
        finally:
            closed = True

    provider.iter_message_pages.return_value = pages()

    @asynccontextmanager
    async def context(*_args: object, **_kwargs: object) -> AsyncIterator[MagicMock]:
        yield provider

    with patch("mailbrief.diagnostics.microsoft.microsoft_provider_context", context):
        await _run_fetch(
            Settings(microsoft_client_id="client-id"),
            _paths(tmp_path),
            silent_only=False,
        )

    assert closed
    assert capsys.readouterr().out == (
        "Connected: user@example.com (Account ID: account)\nMessages in first page: 0\n"
    )


async def test_silent_fetch_never_connects_and_requires_cached_account(tmp_path: Path) -> None:
    provider = MagicMock(spec=MicrosoftEmailProvider)
    provider.current_account = AsyncMock(return_value=None)
    provider.connect = AsyncMock()

    @asynccontextmanager
    async def context(*_args: object, **_kwargs: object) -> AsyncIterator[MagicMock]:
        yield provider

    with (
        patch("mailbrief.diagnostics.microsoft.microsoft_provider_context", context),
        pytest.raises(AuthenticationRequiredError),
    ):
        await _run_fetch(
            Settings(microsoft_client_id="client-id"),
            _paths(tmp_path),
            silent_only=True,
        )
    provider.connect.assert_not_awaited()


def test_disconnect_works_without_settings_or_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    clear = AsyncMock()
    with (
        patch("mailbrief.diagnostics.microsoft.AppPaths.from_qt", return_value=paths),
        patch("mailbrief.diagnostics.microsoft.clear_microsoft_session", clear),
        patch(
            "mailbrief.diagnostics.microsoft.Settings",
            side_effect=AssertionError("Settings must not be built"),
        ),
    ):
        assert main(["disconnect"]) == 0
    clear.assert_awaited_once_with(paths.microsoft_token_cache_path)
    assert capsys.readouterr().out == "Microsoft session removed.\n"


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_stderr"),
    [
        (ConfigurationError("private"), 2, "configuration"),
        (AuthenticationCancelledError("private"), 3, "authentication"),
        (AuthenticationRequiredError("private"), 3, "authentication"),
        (ProviderPermissionError("private"), 4, "permissions"),
        (ProviderRateLimitError("private"), 5, "unavailable"),
        (ProviderError("private"), 5, "unavailable"),
        (ProviderResponseError("private"), 6, "invalid response"),
        (KeyboardInterrupt(), 130, "cancelled"),
        (RuntimeError("private"), 5, "unexpectedly"),
    ],
)
def test_main_maps_failures_by_specific_type(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
    expected_code: int,
    expected_stderr: str,
) -> None:
    with (
        patch("mailbrief.diagnostics.microsoft.AppPaths.from_qt", return_value=_paths(tmp_path)),
        patch("mailbrief.diagnostics.microsoft.Settings", return_value=Settings()),
        patch("mailbrief.diagnostics.microsoft._run_fetch", AsyncMock(side_effect=error)),
    ):
        assert main(["fetch"]) == expected_code
    captured = capsys.readouterr()
    assert expected_stderr in captured.err
    if not isinstance(error, RuntimeError):
        assert "private" not in captured.err
    assert captured.out == ""
