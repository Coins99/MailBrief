"""Refresh credentials stored only in the operating-system credential vault."""

import sys
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from mailbrief.providers.gmail.errors import GmailSetupError

SERVICE = "MailBrief.Gmail"
ENTRY = "personal-account-v1"


class RefreshCredential(BaseModel):
    """Versioned single-account record; never contains access tokens or mail."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    version: int = Field(default=1, ge=1, le=1)
    client_id: str = Field(min_length=1)
    email_address: str = Field(min_length=3)
    refresh_token: SecretStr = Field(min_length=1)


class CredentialStore(Protocol):
    """Synchronous secret-store boundary, called off the application thread."""

    def load(self) -> RefreshCredential | None: ...

    def save(self, credential: RefreshCredential) -> None: ...

    def clear(self) -> None: ...


class VaultBackend(Protocol):
    """Minimal interface for the explicitly selected OS vault backend."""

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def _platform() -> str:
    """Read the platform at call time without letting mypy narrow it per OS."""
    return sys.platform


def os_vault() -> VaultBackend:
    """Choose the OS vault explicitly; never let keyring auto-select a backend."""
    current = _platform()
    if current == "win32":
        try:
            from keyring.backends.Windows import WinVaultKeyring

            return WinVaultKeyring()  # type: ignore[no-untyped-call]
        except Exception:
            raise GmailSetupError("Windows Credential Manager is unavailable.") from None
    if current == "darwin":
        try:
            from keyring.backends.macOS import Keyring as MacOSKeychain

            return MacOSKeychain()  # type: ignore[no-untyped-call]
        except Exception:
            raise GmailSetupError("The macOS Keychain is unavailable.") from None
    raise GmailSetupError("Gmail credential storage requires Windows or macOS.")


class GmailCredentialStore:
    """Fail closed if the OS vault cannot load, save, or delete credentials."""

    def __init__(self, backend: VaultBackend | None = None) -> None:
        self._backend = backend if backend is not None else os_vault()

    def load(self) -> RefreshCredential | None:
        try:
            value = self._backend.get_password(SERVICE, ENTRY)
        except Exception:
            raise GmailSetupError("Unable to read Gmail credentials from the OS vault.") from None
        if value is None:
            return None
        try:
            return RefreshCredential.model_validate_json(value)
        except ValidationError:
            raise GmailSetupError(
                "Stored Gmail credentials are invalid; disconnect and reconnect."
            ) from None

    def save(self, credential: RefreshCredential) -> None:
        # SecretStr's default JSON output is redacted; unwrap only at the vault boundary.
        import json

        payload = credential.model_dump(mode="json")
        payload["refresh_token"] = credential.refresh_token.get_secret_value()
        try:
            self._backend.set_password(SERVICE, ENTRY, json.dumps(payload))
        except Exception:
            raise GmailSetupError("Unable to save Gmail credentials in the OS vault.") from None

    def clear(self) -> None:
        try:
            if self._backend.get_password(SERVICE, ENTRY) is not None:
                self._backend.delete_password(SERVICE, ENTRY)
        except Exception:
            raise GmailSetupError("Unable to remove Gmail credentials from the OS vault.") from None
