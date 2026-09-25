"""Dormant Microsoft provider retained for later Outlook support."""

from mailbrief.providers.microsoft.auth import DEFAULT_AUTHORITY, DEFAULT_SCOPES, MicrosoftAuth
from mailbrief.providers.microsoft.cache import clear_microsoft_session, get_default_token_cache
from mailbrief.providers.microsoft.graph_client import GraphClient
from mailbrief.providers.microsoft.provider import MicrosoftEmailProvider

__all__ = [
    "DEFAULT_AUTHORITY",
    "DEFAULT_SCOPES",
    "GraphClient",
    "MicrosoftAuth",
    "MicrosoftEmailProvider",
    "clear_microsoft_session",
    "get_default_token_cache",
]
