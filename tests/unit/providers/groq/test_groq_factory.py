"""Groq provider composition: settings, the stored key and the HTTP client's lifetime."""

from typing import Any

import httpx
import pytest

from mailbrief.config import Settings
from mailbrief.errors import ConfigurationError
from mailbrief.providers.groq.credentials import ENTRY, SERVICE, GroqKeyError, GroqKeyStore
from mailbrief.providers.groq.factory import groq_provider
from tests.unit.providers.groq.groq_fixtures import TEST_KEY, MemoryVault


def saved_key() -> GroqKeyStore:
    return GroqKeyStore(MemoryVault({(SERVICE, ENTRY): TEST_KEY}))


@pytest.mark.parametrize("model", [None, "   "])
async def test_a_missing_model_is_a_configuration_error(model: str | None) -> None:
    with pytest.raises(ConfigurationError, match="MAILBRIEF_GROQ_MODEL"):
        async with groq_provider(Settings(groq_model=model), key_store=saved_key()):
            pass


async def test_a_missing_key_says_how_to_save_one() -> None:
    no_key = GroqKeyStore(MemoryVault())

    with pytest.raises(GroqKeyError, match="ai-key set"):
        async with groq_provider(Settings(groq_model="test-model"), key_store=no_key):
            pass


async def test_the_http_client_is_pinned_and_closed_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[httpx.AsyncClient] = []

    class RecordingClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", RecordingClient)
    settings = Settings(groq_model=" test-model ", ai_timeout_seconds=45)

    async with groq_provider(settings, key_store=saved_key()) as provider:
        (client,) = created
        assert provider.model_name == "test-model"
        assert not client.is_closed
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert client.timeout == httpx.Timeout(45, connect=10)

    assert client.is_closed


async def test_the_default_key_store_uses_the_os_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mailbrief.providers.groq.credentials.os_vault",
        lambda: MemoryVault({(SERVICE, ENTRY): TEST_KEY}),
    )

    async with groq_provider(Settings(groq_model="test-model")) as provider:
        assert provider.provider_name == "groq"
