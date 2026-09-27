"""Persist only nonsecret desktop configuration, independently of mailbox storage."""

import contextlib
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mailbrief.config import Settings
from mailbrief.errors import ConfigurationError


class DesktopPreferences(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    gmail_oauth_client_path: Path | None = None
    groq_model: str | None = Field(default=None, max_length=128)

    def settings(self) -> Settings:
        """Saved desktop choices override these two environment settings only."""
        return Settings(
            gmail_oauth_client_path=self.gmail_oauth_client_path,
            groq_model=self.groq_model,
        )


class PreferencesStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> DesktopPreferences:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            try:
                settings = Settings()
            except ValidationError:
                raise ConfigurationError("Invalid application configuration.") from None
            return DesktopPreferences(
                gmail_oauth_client_path=settings.gmail_oauth_client_path,
                groq_model=settings.groq_model,
            )
        except (OSError, UnicodeError):
            raise ConfigurationError("Desktop settings could not be read.") from None
        try:
            return DesktopPreferences.model_validate_json(raw)
        except ValidationError:
            raise ConfigurationError("Desktop settings are invalid.") from None

    def save(self, preferences: DesktopPreferences) -> None:
        """Atomic replacement avoids destroying the last good settings on failure."""
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(preferences.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise ConfigurationError("Desktop settings could not be saved.") from None
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)
