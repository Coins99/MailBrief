"""Atomic file writes for exports the owner chooses; nothing else writes draft text to disk."""

import contextlib
import os
import tempfile
from pathlib import Path

_EXISTS = "That file already exists."


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
            os.link(temporary, path)
            os.unlink(temporary)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
