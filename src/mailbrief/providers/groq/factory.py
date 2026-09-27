"""Compose Groq from settings and a key held only in the OS credential vault."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from pydantic import SecretStr

from mailbrief.config import Settings
from mailbrief.providers.groq.credentials import GroqKeyStore
from mailbrief.providers.groq.provider import GroqProvider


@asynccontextmanager
async def groq_provider(
    settings: Settings,
    *,
    key_store: GroqKeyStore | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[GroqProvider]:
    """Keep one request budget and close the HTTP client even on failure/cancellation.

    The model is required now because it is part of the cache identity. The API key is read
    from the vault only when a request is needed, and travels only on those requests.
    """
    model = settings.require_groq_model()

    def read_key() -> SecretStr | None:
        store = key_store if key_store is not None else GroqKeyStore()
        return store.load()

    async def load_key() -> SecretStr | None:
        return await asyncio.to_thread(read_key)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.ai_timeout_seconds, connect=10.0),
        follow_redirects=False,
        trust_env=False,
    ) as http:
        yield GroqProvider(
            http,
            model=model,
            key_loader=load_key,
            max_output_tokens=settings.ai_max_output_tokens,
            max_requests=settings.ai_max_requests_per_run,
            sleep=sleep,
        )
