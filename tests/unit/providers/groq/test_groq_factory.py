"""Groq provider composition: settings, the stored key and the HTTP client's lifetime."""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from mailbrief.config import Settings
from mailbrief.domain.analysis import AnalysisRequest
from mailbrief.domain.messages import EmailContact
from mailbrief.errors import ConfigurationError
from mailbrief.ports.errors import AICredentialsMissingError
from mailbrief.providers.groq.credentials import ENTRY, SERVICE, GroqKeyStore
from mailbrief.providers.groq.factory import groq_provider
from mailbrief.providers.groq.provider import KEY_MISSING_MESSAGE
from tests.unit.providers.groq.groq_fixtures import (
    CHAT_URL,
    TEST_KEY,
    MemoryVault,
    answer_every_message,
)


class CountingVault(MemoryVault):
    """MemoryVault that counts reads and can fail them like a locked OS vault."""

    def __init__(
        self, entries: dict[tuple[str, str], str] | None = None, *, broken: bool = False
    ) -> None:
        super().__init__(entries)
        self.reads = 0
        self.broken = broken

    def get_password(self, service: str, username: str) -> str | None:
        self.reads += 1
        if self.broken:
            raise OSError(f"vault locked for {TEST_KEY}")
        return super().get_password(service, username)


def saved_key() -> GroqKeyStore:
    return GroqKeyStore(MemoryVault({(SERVICE, ENTRY): TEST_KEY}))


@pytest.mark.parametrize("model", [None, "   "])
async def test_a_missing_model_is_a_configuration_error(model: str | None) -> None:
    with pytest.raises(ConfigurationError, match="MAILBRIEF_GROQ_MODEL"):
        async with groq_provider(Settings(groq_model=model), key_store=saved_key()):
            pass


def request() -> AnalysisRequest:
    return AnalysisRequest(
        message_key="0000abcd",
        sender=EmailContact(address="alex@example.com"),
        received_at_utc=datetime(2026, 9, 4, 13, 30, tzinfo=UTC),
        timezone_name="UTC",
        body_text="Please approve the budget.",
    )


async def test_the_key_is_read_once_and_only_when_a_request_needs_it(
    respx_mock: respx.MockRouter,
) -> None:
    vault = CountingVault({(SERVICE, ENTRY): TEST_KEY})
    route = respx_mock.post(CHAT_URL).mock(side_effect=answer_every_message)

    async with groq_provider(
        Settings(groq_model="test-model"), key_store=GroqKeyStore(vault)
    ) as provider:
        assert vault.reads == 0
        assert await provider.credentials_available()
        await provider.analyze([request()])
        await provider.analyze([request()])

    assert vault.reads == 1
    assert [call.request.headers["Authorization"] for call in route.calls] == [
        f"Bearer {TEST_KEY}"
    ] * 2


@pytest.mark.parametrize("broken", [False, True], ids=["missing", "vault-error"])
async def test_without_a_usable_key_nothing_is_sent_and_no_budget_is_used(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture, broken: bool
) -> None:
    vault = CountingVault(broken=broken)
    route = respx_mock.post(CHAT_URL).mock(side_effect=answer_every_message)
    settings = Settings(groq_model="test-model", ai_max_requests_per_run=1)

    async with groq_provider(settings, key_store=GroqKeyStore(vault)) as provider:
        assert not await provider.credentials_available()
        for _ in range(2):  # A spent budget would raise the usage-limit error instead.
            with pytest.raises(AICredentialsMissingError) as caught:
                await provider.analyze([request()])
            assert str(caught.value) == KEY_MISSING_MESSAGE

    assert route.call_count == 0
    assert vault.reads == 1
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert warnings == (
        ["The Groq API key could not be read from the OS credential store."] if broken else []
    )
    assert TEST_KEY not in caplog.text


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
        assert "authorization" not in client.headers
        assert client.timeout == httpx.Timeout(45, connect=10)

    assert client.is_closed


async def test_the_default_key_store_uses_the_os_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mailbrief.providers.groq.credentials.os_vault",
        lambda: MemoryVault({(SERVICE, ENTRY): TEST_KEY}),
    )

    async with groq_provider(Settings(groq_model="test-model")) as provider:
        assert provider.provider_name == "groq"
        assert await provider.credentials_available()
