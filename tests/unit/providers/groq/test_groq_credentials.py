"""Groq API key parsing and OS-vault storage; the key is never echoed."""

import pytest
from pydantic import SecretStr

from mailbrief.errors import ConfigurationError
from mailbrief.providers.groq import credentials
from mailbrief.providers.groq.credentials import (
    ENTRY,
    SERVICE,
    GroqKeyError,
    GroqKeyStore,
    parse_api_key,
)
from tests.unit.providers.groq.groq_fixtures import TEST_KEY, MemoryVault


class BrokenVault:
    """A backend whose every call fails with a message that includes the key."""

    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError(f"vault failure {TEST_KEY}")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError(f"vault failure {password}")

    def delete_password(self, service: str, username: str) -> None:
        raise RuntimeError("vault failure")


def test_the_key_round_trips_through_the_vault() -> None:
    backend = MemoryVault()
    store = GroqKeyStore(backend)

    store.save(parse_api_key(f"  {TEST_KEY}\n"))

    loaded = store.load()
    assert loaded is not None
    assert loaded.get_secret_value() == TEST_KEY
    assert backend.entries == {("MailBrief.Groq", "api-key-v1"): TEST_KEY}
    assert (SERVICE, ENTRY) == ("MailBrief.Groq", "api-key-v1")


def test_clear_is_safe_to_run_twice() -> None:
    store = GroqKeyStore(MemoryVault({(SERVICE, ENTRY): TEST_KEY}))

    store.clear()
    store.clear()

    assert store.load() is None


def test_old_openai_key_is_never_loaded_or_removed() -> None:
    old = ("MailBrief.OpenAI", ENTRY)
    backend = MemoryVault({old: TEST_KEY})
    store = GroqKeyStore(backend)
    assert store.load() is None
    store.save(parse_api_key("gsk_" + "b" * 32))
    store.clear()
    assert backend.entries == {old: TEST_KEY}


@pytest.mark.parametrize("length", [20, 512])
def test_keys_at_the_length_limits_are_accepted(length: int) -> None:
    assert len(parse_api_key("k" * length).get_secret_value()) == length


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "k" * 19,
        "k" * 513,
        "sk-test key-with-a-space-000",
        "sk-test\tkey-with-a-tab-0000",
        "sk-test-ünicode-key-00000000",
    ],
    ids=["empty", "short", "long", "space", "tab", "non-ascii"],
)
def test_invalid_keys_are_rejected_without_echo(raw: str) -> None:
    with pytest.raises(GroqKeyError) as caught:
        parse_api_key(raw)

    assert str(caught.value) == "That does not look like a Groq API key. Nothing was saved."
    assert isinstance(caught.value, ConfigurationError)


@pytest.mark.parametrize("operation", ["load", "save", "clear"])
def test_backend_failures_become_static_errors(operation: str) -> None:
    store = GroqKeyStore(BrokenVault())

    with pytest.raises(GroqKeyError) as caught:
        if operation == "save":
            store.save(SecretStr(TEST_KEY))
        elif operation == "load":
            store.load()
        else:
            store.clear()

    assert TEST_KEY not in str(caught.value)
    assert "OS credential store" in str(caught.value)
    assert caught.value.__cause__ is None


def test_the_default_store_uses_the_os_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MemoryVault({(SERVICE, ENTRY): TEST_KEY})
    monkeypatch.setattr(credentials, "os_vault", lambda: backend)

    loaded = GroqKeyStore().load()

    assert loaded is not None
    assert loaded.get_secret_value() == TEST_KEY
