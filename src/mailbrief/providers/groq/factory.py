"""Compose Groq from settings and a key held only in the OS credential vault."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx

from mailbrief.config import Settings
from mailbrief.providers.groq.credentials import GroqKeyError, GroqKeyStore
from mailbrief.providers.groq.provider import GroqProvider


@asynccontextmanager
async def groq_provider(
    settings: Settings,
    *,
    key_store: GroqKeyStore | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[GroqProvider]:
    """Keep one request budget and close the HTTP client even on failure/cancellation."""
    model = settings.require_groq_model()
    store = key_store if key_store is not None else GroqKeyStore()
    key = await asyncio.to_thread(store.load)
    if key is None:
        raise GroqKeyError("No Groq API key is saved. Run: mailbrief-gmail-diagnostic ai-key set")
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {key.get_secret_value()}"},
        timeout=httpx.Timeout(settings.ai_timeout_seconds, connect=10.0),
        follow_redirects=False,
        trust_env=False,
    ) as http:
        yield GroqProvider(
            http,
            model=model,
            max_output_tokens=settings.ai_max_output_tokens,
            max_requests=settings.ai_max_requests_per_run,
            sleep=sleep,
        )
