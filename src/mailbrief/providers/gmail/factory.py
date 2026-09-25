"""Production composition for Gmail authentication (message adapter follows in M2)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from mailbrief.config import Settings
from mailbrief.providers.gmail.auth import GmailAuth
from mailbrief.providers.gmail.cache import GmailCredentialStore
from mailbrief.providers.gmail.oauth import DesktopClient, LoopbackAuthorization


@asynccontextmanager
async def gmail_auth(settings: Settings) -> AsyncIterator[GmailAuth]:
    """Own network resources and require the Windows OS vault; no token files."""
    client = await asyncio.to_thread(DesktopClient.load, settings.require_gmail_oauth_client_path())
    store = GmailCredentialStore()
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as http:
        yield GmailAuth(client, store, http, LoopbackAuthorization())
