"""Production composition for the Microsoft email provider."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from mailbrief.config import Settings
from mailbrief.paths import AppPaths
from mailbrief.providers.microsoft import MicrosoftEmailProvider
from mailbrief.providers.microsoft.auth import MicrosoftAuth
from mailbrief.providers.microsoft.cache import get_default_token_cache
from mailbrief.providers.microsoft.graph_client import GraphClient


@asynccontextmanager
async def microsoft_provider_context(
    settings: Settings,
    paths: AppPaths,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> AsyncIterator[MicrosoftEmailProvider]:
    """Build and close the Microsoft provider's production dependencies."""
    client_id = settings.require_microsoft_client_id()
    cache = await asyncio.to_thread(get_default_token_cache, paths.microsoft_token_cache_path)
    auth = await MicrosoftAuth.create(client_id, token_cache=cache)
    graph_client = GraphClient(
        auth,
        base_url=str(settings.graph_base_url),
        http_client=http_client,
    )
    async with graph_client:
        yield MicrosoftEmailProvider(auth, graph_client)
