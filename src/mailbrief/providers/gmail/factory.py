"""Production composition for Gmail authentication and metadata synchronization."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from mailbrief.config import Settings
from mailbrief.errors import ConfigurationError
from mailbrief.providers.gmail.auth import GmailAuth
from mailbrief.providers.gmail.cache import GmailCredentialStore
from mailbrief.providers.gmail.client import GmailClient
from mailbrief.providers.gmail.errors import GmailSetupError
from mailbrief.providers.gmail.oauth import DesktopClient, LoopbackAuthorization
from mailbrief.providers.gmail.provider import GmailProvider


@asynccontextmanager
async def gmail_auth(settings: Settings) -> AsyncIterator[GmailAuth]:
    """Own network resources and require the OS credential vault; no token files."""
    try:
        path = settings.require_gmail_oauth_client_path()
    except ConfigurationError:
        raise GmailSetupError(
            "Set MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH in this terminal "
            "to your Desktop OAuth JSON file."
        ) from None
    client = await asyncio.to_thread(DesktopClient.load, path)
    store = GmailCredentialStore()
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as http:
        yield GmailAuth(client, store, http, LoopbackAuthorization())


@asynccontextmanager
async def gmail_provider(
    settings: Settings, *, silent_only: bool = False
) -> AsyncIterator[GmailProvider]:
    """Compose metadata access without exposing provider resources to core services."""
    async with (
        gmail_auth(settings) as auth,
        httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as http,
    ):
        yield GmailProvider(auth, GmailClient(http, auth), silent_only=silent_only)
