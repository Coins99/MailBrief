"""OS-store isolation, redaction and fail-closed persistence tests."""

import sys

import pytest
from pydantic import SecretStr

from mailbrief.errors import ConfigurationError
from mailbrief.providers.gmail import cache
from mailbrief.providers.gmail.cache import GmailCredentialStore, RefreshCredential


class MemoryVault:
    def __init__(self) -> None:
        self.value: str | None = None
        self.fail = False

    def get_password(self, service: str, username: str) -> str | None:
        assert (service, username) == (cache.SERVICE, cache.ENTRY)
        if self.fail:
            raise RuntimeError("sensitive vault details")
        return self.value

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.fail:
            raise RuntimeError("sensitive vault details")
        self.value = password

    def delete_password(self, service: str, username: str) -> None:
        if self.fail:
            raise RuntimeError("sensitive vault details")
        self.value = None


def credential() -> RefreshCredential:
    return RefreshCredential(
        client_id="client",
        email_address="me@example.com",
        refresh_token=SecretStr("secret-refresh"),
    )


def test_roundtrip_and_idempotent_clear() -> None:
    vault = MemoryVault()
    store = GmailCredentialStore(vault)
    assert store.load() is None
    store.save(credential())
    restored = store.load()
    assert restored is not None
    assert restored.refresh_token.get_secret_value() == "secret-refresh"
    assert "secret-refresh" not in repr(restored)
    store.clear()
    store.clear()
    assert store.load() is None


@pytest.mark.parametrize("operation", ["load", "save", "clear"])
def test_storage_failure_is_safe(operation: str) -> None:
    vault = MemoryVault()
    store = GmailCredentialStore(vault)
    vault.fail = True
    with pytest.raises(ConfigurationError) as error:
        if operation == "save":
            store.save(credential())
        elif operation == "load":
            store.load()
        else:
            store.clear()
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize(
    "value", ["not-json", '{"refresh_token":"secret-refresh"}', '{"version":2}']
)
def test_corrupt_storage_does_not_expose_payload(value: str) -> None:
    vault = MemoryVault()
    vault.value = value
    with pytest.raises(ConfigurationError, match="disconnect"):
        GmailCredentialStore(vault).load()


def test_reject_non_windows_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ConfigurationError, match="Windows"):
        cache.windows_vault()
