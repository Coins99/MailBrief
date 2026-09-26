"""OS vault selection: explicit backends, static errors and no plaintext fallback."""

import sys

import pytest

from mailbrief.errors import ConfigurationError
from mailbrief.infra import vault
from mailbrief.infra.vault import VaultUnavailableError, os_vault


def test_windows_uses_the_credential_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    from keyring.backends.Windows import WinVaultKeyring

    monkeypatch.setattr(vault, "_platform", lambda: "win32")

    assert isinstance(os_vault(), WinVaultKeyring)


def test_macos_uses_the_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    from keyring.backends.macOS import Keyring

    monkeypatch.setattr(vault, "_platform", lambda: "darwin")

    assert isinstance(os_vault(), Keyring)


@pytest.mark.parametrize("platform", ["linux", "freebsd14", "cygwin"])
def test_other_platforms_have_no_vault(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(vault, "_platform", lambda: platform)

    with pytest.raises(VaultUnavailableError, match="requires Windows or macOS") as caught:
        os_vault()

    assert isinstance(caught.value, ConfigurationError)


@pytest.mark.parametrize(
    ("platform", "target", "message"),
    [
        ("win32", "keyring.backends.Windows.WinVaultKeyring", "Windows Credential Manager"),
        ("darwin", "keyring.backends.macOS.Keyring", "macOS Keychain"),
    ],
)
def test_an_unusable_backend_is_reported_without_details(
    monkeypatch: pytest.MonkeyPatch, platform: str, target: str, message: str
) -> None:
    def broken() -> None:
        raise RuntimeError("backend detail")

    monkeypatch.setattr(vault, "_platform", lambda: platform)
    monkeypatch.setattr(target, broken)

    with pytest.raises(VaultUnavailableError, match=message) as caught:
        os_vault()

    assert "backend detail" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_the_platform_is_read_at_call_time() -> None:
    assert vault._platform() == sys.platform
