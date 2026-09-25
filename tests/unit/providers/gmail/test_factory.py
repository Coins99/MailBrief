"""Production composition owns and closes HTTP resources without touching a real vault."""

import json
from pathlib import Path

import pytest

from mailbrief.config import Settings
from mailbrief.errors import ConfigurationError
from mailbrief.providers.gmail import factory
from mailbrief.providers.gmail.cache import GmailCredentialStore
from tests.unit.providers.gmail.test_cache import MemoryVault


async def test_factory_closes_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "fake.apps.googleusercontent.com",
                    "client_secret": "fake-secret",
                }
            }
        )
    )
    monkeypatch.setattr(
        factory, "GmailCredentialStore", lambda: GmailCredentialStore(MemoryVault())
    )
    async with factory.gmail_auth(Settings(gmail_oauth_client_path=path)) as auth:
        http = auth._http
        assert not http.is_closed
        assert not http.follow_redirects
    assert http.is_closed


async def test_factory_requires_client_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH", raising=False)
    with pytest.raises(ConfigurationError, match="MAILBRIEF_GMAIL_OAUTH_CLIENT_PATH"):
        async with factory.gmail_auth(Settings()) as _:
            pass
