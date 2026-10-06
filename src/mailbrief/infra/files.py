"""Atomic file writes for exports the owner chooses; nothing else writes draft text to disk."""

import contextlib
import ctypes
import errno
import os
import shutil
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


# Errors meaning "this filesystem can't rename exclusively or hard-link" (exFAT, FAT32 and
# some network shares). EEXIST is deliberately absent: an existing destination is refused.
_UNSUPPORTED = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.EINVAL, errno.ENOSYS})


def publish_exclusively(source: str, destination: Path) -> None:
    """Publish ``source`` at ``destination`` without ever replacing an existing file.

    Filesystems without an exclusive rename or hard links get an exclusive copy instead.
    """
    try:
        _rename_exclusively(source, destination)
    except OSError as error:
        if error.errno not in _UNSUPPORTED:
            raise  # Includes FileExistsError: an existing destination is never replaced.
        _copy_exclusively(source, destination)


def _rename_exclusively(source: str, destination: Path) -> None:
    """Atomically publish a file without replacing a concurrent writer's file."""
    if sys.platform == "win32":
        os.rename(source, destination)
    elif sys.platform == "darwin":
        # Darwin RENAME_EXCL needs no hard links, but only some volumes support it.
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


def _copy_exclusively(source: str, destination: Path) -> None:
    """Exclusive but not atomic: O_EXCL refuses an existing file and 0600 keeps it private.

    A crash mid-copy can leave a partial file; a partial backup fails verification.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output, open(source, "rb") as incoming:
            shutil.copyfileobj(incoming, output, 1024 * 1024)
            output.flush()
            # On disk before the only other copy is removed.
            os.fsync(output.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(destination)  # O_EXCL succeeded, so this partial file is ours.
        raise
    # Published and flushed: the copy has succeeded, and deleting the source is cleanup.
    # The callers' own cleanup tries again; a source that still can't go is left behind.
    with contextlib.suppress(OSError):
        os.unlink(source)


def write_text_atomically(path: Path, text: str, *, overwrite: bool) -> None:
    """Write ``text`` as UTF-8 through a temporary file in the same folder, then move it
    into place, so a crash never leaves a half-written file at ``path``.

    Line breaks are written as they are. With ``overwrite`` false an existing file is
    refused with FileExistsError; on a filesystem without an exclusive rename or hard links
    the new file is copied exclusively instead, which a crash can leave partial. A
    temporary file that can't be deleted never fails a finished write. Errors carry no file
    content.
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
    finally:
        # Already gone after a rename; after a copy, a second try at a delete that failed.
        with contextlib.suppress(OSError):
            os.unlink(temporary)
