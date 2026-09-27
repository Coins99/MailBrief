"""The Groq API key, kept only in the OS credential vault; no message ever contains it."""

from pydantic import SecretStr

from mailbrief.errors import ConfigurationError
from mailbrief.infra.vault import VaultBackend, os_vault

SERVICE = "MailBrief.Groq"
ENTRY = "api-key-v1"
_MIN_KEY_CHARS = 20
_MAX_KEY_CHARS = 512


class GroqKeyError(ConfigurationError):
    """The Groq API key is missing, malformed or unavailable. Messages are static."""


def parse_api_key(raw: str) -> SecretStr:
    """Accept 20-512 printable ASCII characters without whitespace; never echo the value."""
    key = raw.strip()
    if not _MIN_KEY_CHARS <= len(key) <= _MAX_KEY_CHARS or any(
        not 33 <= ord(character) <= 126 for character in key
    ):
        raise GroqKeyError("That does not look like a Groq API key. Nothing was saved.")
    return SecretStr(key)


class GroqKeyStore:
    """Load, save and clear the key in the OS vault."""

    def __init__(self, backend: VaultBackend | None = None) -> None:
        self._backend = backend if backend is not None else os_vault()

    def load(self) -> SecretStr | None:
        try:
            value = self._backend.get_password(SERVICE, ENTRY)
        except Exception:
            raise GroqKeyError(
                "The Groq API key could not be read from the OS credential store."
            ) from None
        return None if value is None else SecretStr(value)

    def save(self, key: SecretStr) -> None:
        try:
            self._backend.set_password(SERVICE, ENTRY, key.get_secret_value())
        except Exception:
            raise GroqKeyError(
                "The Groq API key could not be saved in the OS credential store."
            ) from None

    def clear(self) -> None:
        """Remove the key; clearing an absent key succeeds."""
        try:
            if self._backend.get_password(SERVICE, ENTRY) is not None:
                self._backend.delete_password(SERVICE, ENTRY)
        except Exception:
            raise GroqKeyError(
                "The Groq API key could not be removed from the OS credential store."
            ) from None
