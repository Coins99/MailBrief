"""Refresh credentials stored only in Windows Credential Manager."""

import sys
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from mailbrief.errors import ConfigurationError

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


def windows_vault() -> VaultBackend:
    """Avoid keyring auto-selection, including insecure third-party backends."""
    if sys.platform != "win32":
        raise ConfigurationError("Gmail credential storage currently requires Windows.")
    try:
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()  # type: ignore[no-untyped-call]
    except Exception:
        raise ConfigurationError("Windows Credential Manager is unavailable.") from None


class GmailCredentialStore:
    """Fail closed if the OS vault cannot load, save, or delete credentials."""

    def __init__(self, backend: VaultBackend | None = None) -> None:
        self._backend = backend if backend is not None else windows_vault()

    def load(self) -> RefreshCredential | None:
        try:
            value = self._backend.get_password(SERVICE, ENTRY)
        except Exception:
            raise ConfigurationError(
                "Unable to read Gmail credentials from the OS vault."
            ) from None
        if value is None:
            return None
        try:
            return RefreshCredential.model_validate_json(value)
        except ValidationError:
            raise ConfigurationError(
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
            raise ConfigurationError("Unable to save Gmail credentials in the OS vault.") from None

    def clear(self) -> None:
        try:
            if self._backend.get_password(SERVICE, ENTRY) is not None:
                self._backend.delete_password(SERVICE, ENTRY)
        except Exception:
            raise ConfigurationError(
                "Unable to remove Gmail credentials from the OS vault."
            ) from None
