"""Atomic file writes for exports the owner chooses; nothing else writes draft text to disk."""

import contextlib
import ctypes
import os
import sys
import tempfile
from pathlib import Path

_EXISTS = "That file already exists."


def sync_directory(path: Path) -> None:
    """Persist directory entries on POSIX; Windows has no directory fsync API."""
    if sys.platform != "win32":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def publish_exclusively(source: str, destination: Path) -> None:
    """Atomically publish a file without replacing a concurrent writer's file."""
    if sys.platform == "win32":
        os.rename(source, destination)
    elif sys.platform == "darwin":
        # Darwin RENAME_EXCL works without requiring hard-link support.
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(os.fsencode(source), os.fsencode(destination), 0x00000004) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), destination)
    else:
        os.link(source, destination)
        os.unlink(source)


def write_text_atomically(path: Path, text: str, *, overwrite: bool) -> None:
    """Write ``text`` as UTF-8 through a temporary file in the same folder, then move it
    into place, so a crash never leaves a half-written file at ``path``.

    Line breaks are written as they are. With ``overwrite`` false an existing file is
    refused with FileExistsError. Errors carry no file content.
    """
    if not overwrite and path.exists():
        raise FileExistsError(_EXISTS)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mailbrief-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(text.encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Exclusive publication also refuses a destination created during the write.
            publish_exclusively(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
