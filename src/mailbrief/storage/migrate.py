"""Run checked-in Alembic upgrades against the explicit diagnostic database."""

import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from mailbrief.errors import ConfigurationError
from mailbrief.storage.database import sqlite_url


def migration_config(path: Path) -> Config:
    """Resolve bundled revisions and bind them to an explicit database path."""
    bundle = getattr(sys, "_MEIPASS", None)
    root = Path(bundle) if isinstance(bundle, str) else Path(__file__).resolve().parents[3]
    if not (root / "migrations" / "env.py").is_file():
        raise ConfigurationError(
            "Migration resources are unavailable; reinstall MailBrief or use the project checkout."
        )
    config = Config()
    config.set_main_option("script_location", str(root / "migrations").replace("%", "%%"))
    config.set_main_option("sqlalchemy.url", sqlite_url(path).replace("%", "%%"))
    config.attributes["explicit_database_url"] = sqlite_url(path)
    return config


def upgrade_database(path: Path) -> None:
    """Use read-only bundled revisions; always migrate the explicit user-data database."""
    config = migration_config(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size:
        with closing(sqlite3.connect(path)) as source:
            has_version = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
            ).fetchone()
            versions = (
                {row[0] for row in source.execute("SELECT version_num FROM alembic_version")}
                if has_version
                else set()
            )
            scripts = ScriptDirectory.from_config(config)
            for version in versions:
                # Reject newer/unknown schemas without modifying them.
                scripts.get_revision(version)
            if versions != set(scripts.get_heads()):
                # SQLite's backup API includes committed WAL pages. A raw file copy does not.
                # Unique files preserve the original even after a partially failed upgrade/retry.
                with tempfile.NamedTemporaryFile(
                    dir=path.parent,
                    prefix=f"{path.name}.pre-upgrade-",
                    suffix=".sqlite3",
                    delete=False,
                ) as temporary:
                    backup = Path(temporary.name)
                try:
                    with closing(sqlite3.connect(backup)) as destination:
                        source.backup(destination)
                except BaseException:
                    backup.unlink(missing_ok=True)
                    raise
    command.upgrade(config, "head")
