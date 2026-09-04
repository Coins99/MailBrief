"""Tests for Microsoft provider production composition."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mailbrief.config import Settings
from mailbrief.errors import ConfigurationError
from mailbrief.paths import AppPaths
from mailbrief.providers.microsoft import MicrosoftEmailProvider
from mailbrief.providers.microsoft.factory import microsoft_provider_context


def _paths(root: Path) -> AppPaths:
    return AppPaths(root, root / "mailbrief.sqlite3", root / "tokens.bin")


async def test_factory_builds_in_order_and_preserves_injected_client(tmp_path: Path) -> None:
    cache = MagicMock()
    auth = MagicMock()
    graph = MagicMock()
    graph.__aenter__ = AsyncMock(return_value=graph)
    graph.__aexit__ = AsyncMock(return_value=None)
    injected = MagicMock(spec=httpx.AsyncClient)

    with (
        patch(
            "mailbrief.providers.microsoft.factory.get_default_token_cache",
            return_value=cache,
        ) as cache_factory,
        patch(
            "mailbrief.providers.microsoft.factory.MicrosoftAuth.create",
            AsyncMock(return_value=auth),
        ) as auth_factory,
        patch(
            "mailbrief.providers.microsoft.factory.GraphClient",
            return_value=graph,
        ) as graph_factory,
    ):
        async with microsoft_provider_context(
            Settings(microsoft_client_id="client-id"),
            _paths(tmp_path),
            http_client=injected,
        ) as provider:
            assert isinstance(provider, MicrosoftEmailProvider)

    cache_factory.assert_called_once_with(tmp_path / "tokens.bin")
    auth_factory.assert_awaited_once_with(
        "client-id",
        token_cache=cache,
        cache_path=tmp_path / "tokens.bin",
    )
    graph_factory.assert_called_once_with(
        auth,
        base_url="https://graph.microsoft.com/v1.0",
        http_client=injected,
    )
    graph.__aexit__.assert_awaited_once()


async def test_factory_closes_graph_context_on_consumer_failure(tmp_path: Path) -> None:
    graph = MagicMock()
    graph.__aenter__ = AsyncMock(return_value=graph)
    graph.__aexit__ = AsyncMock(return_value=None)
    with (
        patch("mailbrief.providers.microsoft.factory.get_default_token_cache"),
        patch(
            "mailbrief.providers.microsoft.factory.MicrosoftAuth.create",
            AsyncMock(return_value=MagicMock()),
        ),
        patch("mailbrief.providers.microsoft.factory.GraphClient", return_value=graph),
        pytest.raises(RuntimeError, match="consumer"),
    ):
        async with microsoft_provider_context(
            Settings(microsoft_client_id="client-id"),
            _paths(tmp_path),
        ):
            raise RuntimeError("consumer failed")
    graph.__aexit__.assert_awaited_once()


async def test_factory_rejects_missing_client_id_before_cache(tmp_path: Path) -> None:
    with (
        patch("mailbrief.providers.microsoft.factory.get_default_token_cache") as cache_factory,
        pytest.raises(ConfigurationError, match="MICROSOFT_CLIENT_ID"),
    ):
        async with microsoft_provider_context(Settings(), _paths(tmp_path)):
            pass
    cache_factory.assert_not_called()
