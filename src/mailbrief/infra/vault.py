"""The operating system's credential vault, chosen explicitly; there is no plaintext fallback."""

import sys
from typing import Protocol

from mailbrief.errors import ConfigurationError


class VaultBackend(Protocol):
    """The keyring operations MailBrief uses."""

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class VaultUnavailableError(ConfigurationError):
    """The OS credential vault cannot be used. Messages are static."""


def _platform() -> str:
    """Read the platform at call time without letting mypy narrow it per OS."""
    return sys.platform


def os_vault() -> VaultBackend:
    """Windows Credential Manager or the macOS Keychain; keyring never auto-selects a backend."""
    current = _platform()
    if current == "win32":
        try:
            from keyring.backends.Windows import WinVaultKeyring

            return WinVaultKeyring()  # type: ignore[no-untyped-call]
        except Exception:
            raise VaultUnavailableError("Windows Credential Manager is unavailable.") from None
    if current == "darwin":
        try:
            from keyring.backends.macOS import Keyring as MacOSKeychain

            return MacOSKeychain()  # type: ignore[no-untyped-call]
        except Exception:
            raise VaultUnavailableError("The macOS Keychain is unavailable.") from None
    raise VaultUnavailableError("Secure credential storage requires Windows or macOS.")
