"""Coordinate offline recovery with the desktop and diagnostic CLI."""

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from PySide6.QtCore import QLockFile

from mailbrief.errors import ConfigurationError


class DataInUseError(ConfigurationError):
    """A data folder cannot be used exclusively by this operation."""


@contextmanager
def data_directory_lock(database: Path) -> Iterator[None]:
    """Hold the desktop's lock until database operations and shutdown finish."""
    directory = database.resolve().parent
    created: list[Path] = []
    ancestor = directory
    while not ancestor.exists() and ancestor != ancestor.parent:
        created.append(ancestor)
        ancestor = ancestor.parent
    lock = None
    acquired = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(directory / "desktop.lock"))
        lock.setStaleLockTime(0)
        acquired = lock.tryLock(0)
        if not acquired:
            raise DataInUseError(
                "MailBrief's data folder is busy or unavailable. Close the desktop and other "
                "diagnostic commands, check folder access, then retry."
            )
        yield
    finally:
        if acquired and lock is not None:
            lock.unlock()
        # Failed sign-in must not leave a new profile directory behind. Never remove
        # existing directories or directories to which a client has written anything.
        for path in created:
            with suppress(OSError):
                path.rmdir()
