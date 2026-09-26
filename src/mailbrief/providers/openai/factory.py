"""Compose the OpenAI provider from settings and the OS-stored API key."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import openai

from mailbrief.config import Settings
from mailbrief.providers.openai.credentials import OpenAIKeyError, OpenAIKeyStore
from mailbrief.providers.openai.provider import OpenAIProvider

OPENAI_BASE_URL = "https://api.openai.com/v1"
_CONNECT_TIMEOUT_SECONDS = 10.0


@asynccontextmanager
async def openai_provider(
    settings: Settings,
    *,
    key_store: OpenAIKeyStore | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[OpenAIProvider]:
    """Yield a provider pinned to api.openai.com; the HTTP client closes on exit.

    Pinning base_url means OPENAI_BASE_URL can never redirect email content, and
    trust_env=False keeps proxy environment variables out. SDK retries are off because
    the provider classifies and retries failures itself.
    """
    model = settings.require_openai_model()
    store = key_store if key_store is not None else OpenAIKeyStore()
    key = await asyncio.to_thread(store.load)
    if key is None:
        raise OpenAIKeyError(
            "No OpenAI API key is saved. Run: mailbrief-gmail-diagnostic ai-key set"
        )
    seconds = settings.ai_timeout_seconds
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(seconds, connect=_CONNECT_TIMEOUT_SECONDS),
        follow_redirects=False,
        trust_env=False,
    ) as http:
        client = openai.AsyncOpenAI(
            api_key=key.get_secret_value(),
            base_url=OPENAI_BASE_URL,
            # The SDK accepts httpx clients at runtime, but annotates only its httpx2 client.
            http_client=cast(Any, http),
            max_retries=0,
            timeout=openai.Timeout(seconds, connect=_CONNECT_TIMEOUT_SECONDS),
        )
        yield OpenAIProvider(
            client, model=model, max_output_tokens=settings.ai_max_output_tokens, sleep=sleep
        )
