"""Fixtures every CLI integration test gets: the OS vault's Gmail entry is always a fake."""

import json

import pytest

from mailbrief.providers.gmail.cache import ENTRY, SERVICE
from tests.unit.providers.gmail.metadata_fixtures import ACCOUNT
from tests.unit.providers.groq.groq_fixtures import MemoryVault


@pytest.fixture(autouse=True)
def gmail_vault(monkeypatch: pytest.MonkeyPatch) -> MemoryVault:
    """The stored Gmail credential of the account FakeSession connects as. Commands that
    need the connected account without connecting read it from here, never from the real
    vault; clear ``entries`` for a test with no Gmail connected."""
    stored = {
        "version": 1,
        "client_id": "client",
        "email_address": ACCOUNT.email_address,
        "refresh_token": "synthetic-refresh-token",
    }
    backend = MemoryVault({(SERVICE, ENTRY): json.dumps(stored)})
    monkeypatch.setattr("mailbrief.providers.gmail.cache.os_vault", lambda: backend)
    return backend
