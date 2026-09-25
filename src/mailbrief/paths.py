"""Stable application-data paths shared by runtime composition roots."""

from dataclasses import dataclass
from pathlib import Path
from typing import Self

from PySide6.QtCore import QCoreApplication, QStandardPaths

from mailbrief import __version__
from mailbrief.errors import ConfigurationError


def configure_qt_identity() -> None:
    """Configure the identity Qt uses when resolving platform data paths."""
    QCoreApplication.setOrganizationName("MailBrief")
    QCoreApplication.setApplicationName("MailBrief")
    QCoreApplication.setApplicationVersion(__version__)


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Filesystem paths owned by the MailBrief application."""

    data_dir: Path
    database_path: Path
    microsoft_token_cache_path: Path

    @classmethod
    def from_qt(cls) -> Self:
        """Resolve and create the platform-specific local application-data root."""
        configure_qt_identity()
        raw_location = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        if not raw_location:
            raise ConfigurationError("Unable to resolve the application data directory.")

        data_dir = Path(raw_location).expanduser().resolve(strict=False)
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigurationError(
                "Unable to initialize the application data directory."
            ) from exc

        return cls(
            data_dir=data_dir,
            database_path=data_dir / "mailbrief.sqlite3",
            microsoft_token_cache_path=data_dir / "msal-token-cache.bin",
        )
