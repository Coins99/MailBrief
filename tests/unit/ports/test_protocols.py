"""Tests that adapters can satisfy provider protocols structurally."""

from collections.abc import AsyncIterator, Sequence
from datetime import datetime

from mailbrief.domain.analysis import AnalysisRequest, AnalysisResponse
from mailbrief.domain.bodies import BodySource, MessageBody
from mailbrief.domain.messages import AccountIdentity, MessagePage, ProviderKind
from mailbrief.ports.ai_provider import AIProvider
from mailbrief.ports.email_provider import EmailProvider
from mailbrief.ports.errors import ProviderRateLimitError


class FakeEmailProvider:
    """Minimal structural implementation used by service tests."""

    @property
    def provider_kind(self) -> ProviderKind:
        return ProviderKind.MICROSOFT

    async def connect(self) -> AccountIdentity:
        return AccountIdentity(
            provider=ProviderKind.MICROSOFT,
            provider_account_id="account-1",
            email_address="taylor@example.com",
        )

    async def current_account(self) -> AccountIdentity | None:
        return await self.connect()

    async def iter_message_pages(
        self,
        *,
        range_start_utc: datetime,
        range_end_utc: datetime,
        continuation: str | None = None,
    ) -> AsyncIterator[MessagePage]:
        del range_start_utc, range_end_utc, continuation
        yield MessagePage(page_number=1, messages=())

    async def fetch_message_body(self, provider_message_id: str) -> MessageBody:
        return MessageBody(
            provider_message_id=provider_message_id,
            text=f"Body for {provider_message_id}",
            source=BodySource.PLAIN,
        )

    async def disconnect(self) -> None:
        return None


class FakeAIProvider:
    """Minimal structural AI implementation used by service tests."""

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model"

    @property
    def prompt_version(self) -> str:
        return "fake-prompt-1"

    async def credentials_available(self) -> bool:
        return True

    async def analyze(self, requests: Sequence[AnalysisRequest]) -> AnalysisResponse:
        del requests
        return AnalysisResponse()


def test_email_adapter_satisfies_runtime_protocol() -> None:
    provider: EmailProvider = FakeEmailProvider()

    assert isinstance(provider, EmailProvider)
    assert provider.provider_kind is ProviderKind.MICROSOFT


def test_ai_adapter_satisfies_runtime_protocol() -> None:
    provider: AIProvider = FakeAIProvider()

    assert isinstance(provider, AIProvider)
    assert provider.provider_name == "fake"
    assert provider.model_name == "fake-model"
    assert provider.prompt_version == "fake-prompt-1"


def test_rate_limit_error_carries_retry_delay() -> None:
    error = ProviderRateLimitError("slow down", retry_after_seconds=12.5)

    assert str(error) == "slow down"
    assert error.retry_after_seconds == 12.5
