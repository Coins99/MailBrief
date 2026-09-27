"""Run checked-in Alembic upgrades against the explicit diagnostic database."""

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from mailbrief.errors import ConfigurationError
from mailbrief.storage.database import sqlite_url


def upgrade_database(path: Path) -> None:
    """Use read-only bundled revisions; always migrate the explicit user-data database."""
    bundle = getattr(sys, "_MEIPASS", None)
    root = Path(bundle) if isinstance(bundle, str) else Path(__file__).resolve().parents[3]
    if not (root / "migrations" / "env.py").is_file():
        raise ConfigurationError(
            "Migration resources are unavailable; reinstall MailBrief or use the project checkout."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    config = Config()
    config.set_main_option("script_location", str(root / "migrations").replace("%", "%%"))
    config.set_main_option("sqlalchemy.url", sqlite_url(path).replace("%", "%%"))
    config.attributes["explicit_database_url"] = sqlite_url(path)
    command.upgrade(config, "head")
