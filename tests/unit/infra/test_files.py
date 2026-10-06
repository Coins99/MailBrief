"""Atomic exports: UTF-8 bytes as given, no partial files, and optional overwrite refusal."""

import ctypes
import errno
import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from mailbrief.infra import files
from mailbrief.infra.files import publish_exclusively, sync_directory, write_text_atomically


def test_writes_utf8_with_line_breaks_as_given(tmp_path: Path) -> None:
    target = tmp_path / "draft.txt"

    write_text_atomically(target, "Café\r\nnaïve\n", overwrite=False)

    assert target.read_bytes() == "Café\r\nnaïve\n".encode()
    assert list(tmp_path.iterdir()) == [target]


def test_overwrite_replaces_and_refusal_keeps_the_old_file(tmp_path: Path) -> None:
    target = tmp_path / "draft.md"
    target.write_text("old", encoding="utf-8")

    with pytest.raises(FileExistsError, match="^That file already exists.$"):
        write_text_atomically(target, "new", overwrite=False)
    assert target.read_text(encoding="utf-8") == "old"

    write_text_atomically(target, "new", overwrite=True)
    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.iterdir()) == [target]


def test_a_file_created_during_the_write_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "draft.md"
    real_fsync = os.fsync

    def fsync_then_race(descriptor: int) -> None:
        real_fsync(descriptor)
        target.write_text("theirs", encoding="utf-8")

    monkeypatch.setattr(os, "fsync", fsync_then_race)

    with pytest.raises(FileExistsError):
        write_text_atomically(target, "mine", overwrite=False)
    assert target.read_text(encoding="utf-8") == "theirs"
    assert list(tmp_path.iterdir()) == [target]


def test_a_failed_write_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_source: object, _target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)

    with pytest.raises(OSError, match="disk full"):
        write_text_atomically(tmp_path / "draft.md", "text", overwrite=True)
    assert list(tmp_path.iterdir()) == []


def test_a_missing_folder_fails_without_a_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        write_text_atomically(tmp_path / "missing" / "draft.md", "text", overwrite=True)


def test_directory_sync_closes_descriptor_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files, "sys", SimpleNamespace(platform="darwin"))
    closed: list[int] = []
    monkeypatch.setattr(os, "open", lambda path, flags: 91)
    monkeypatch.setattr(os, "close", closed.append)

    def fail(descriptor: int) -> None:
        assert descriptor == 91
        raise OSError("sync failed")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="sync failed"):
        sync_directory(tmp_path)
    assert closed == [91]


@pytest.mark.skipif(sys.platform not in ("win32", "darwin"), reason="Supported native platforms")
def test_export_does_not_require_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(source: object, destination: object) -> None:
        raise OSError("hard links unsupported")

    monkeypatch.setattr(os, "link", fail)
    target = tmp_path / "draft.txt"
    write_text_atomically(target, "owner writing", overwrite=False)
    assert target.read_text() == "owner writing"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("error", [errno.EEXIST, errno.EACCES])
def test_darwin_publication_error_preserves_destination_and_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    target = tmp_path / "draft.txt"

    def race(source: bytes, destination: bytes, flags: int) -> int:
        assert destination == os.fsencode(target) and flags == 4
        target.write_text("theirs")
        ctypes.set_errno(error)
        return -1

    rename = Mock(side_effect=race)
    monkeypatch.setattr(files, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(ctypes, "CDLL", Mock(return_value=SimpleNamespace(renamex_np=rename)))
    with pytest.raises(OSError) as caught:
        write_text_atomically(target, "mine", overwrite=False)
    assert caught.value.errno == error
    assert target.read_text() == "theirs"
    assert list(tmp_path.iterdir()) == [target]


def unsupported(source: object, destination: object) -> None:
    raise OSError(errno.ENOTSUP, "Operation not supported")


def test_publication_copies_where_the_filesystem_cannot_rename_exclusively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "staged.tmp"
    source.write_bytes(b"owner writing\r\n")
    target = tmp_path / "export.txt"
    monkeypatch.setattr(files, "_rename_exclusively", unsupported)

    publish_exclusively(str(source), target)

    assert target.read_bytes() == b"owner writing\r\n"
    assert not source.exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize("fast_path", ["refuses", "unsupported"])
def test_publication_never_replaces_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_path: str
) -> None:
    source = tmp_path / "staged.tmp"
    source.write_bytes(b"mine")
    target = tmp_path / "export.txt"
    target.write_bytes(b"theirs")
    if fast_path == "unsupported":
        monkeypatch.setattr(files, "_rename_exclusively", unsupported)

    with pytest.raises(FileExistsError):
        publish_exclusively(str(source), target)

    assert target.read_bytes() == b"theirs"
    assert source.read_bytes() == b"mine"


def test_a_failed_fallback_copy_leaves_no_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "staged.tmp"
    source.write_bytes(b"mine")
    target = tmp_path / "export.txt"
    monkeypatch.setattr(files, "_rename_exclusively", unsupported)

    def full(*args: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copyfileobj", full)
    with pytest.raises(OSError) as caught:
        publish_exclusively(str(source), target)

    assert caught.value.errno == errno.ENOSPC
    assert not target.exists()
    assert source.read_bytes() == b"mine"


@pytest.mark.parametrize("error", [errno.EEXIST, errno.EACCES])
def test_other_publication_errors_never_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    def fail(source: object, destination: object) -> None:
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(files, "_rename_exclusively", fail)
    monkeypatch.setattr(files, "_copy_exclusively", Mock(side_effect=AssertionError))
    with pytest.raises(OSError) as caught:
        publish_exclusively(str(tmp_path / "staged.tmp"), tmp_path / "export.txt")
    assert caught.value.errno == error


def test_export_falls_back_on_a_filesystem_without_exclusive_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files, "_rename_exclusively", unsupported)
    target = tmp_path / "draft.txt"

    write_text_atomically(target, "owner writing", overwrite=False)

    assert target.read_text(encoding="utf-8") == "owner writing"
    assert list(tmp_path.iterdir()) == [target]


def locked(names: Callable[[str], bool], *, times: int | None = None) -> Callable[..., None]:
    """os.unlink failing like a file another process holds open, ``times`` times or always."""
    real = os.unlink
    failures: list[str] = []

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        name = os.path.basename(os.fspath(path))
        if names(name) and (times is None or len(failures) < times):
            failures.append(name)
            raise PermissionError(errno.EACCES, "Permission denied", path)
        real(path, *args, **kwargs)

    return unlink


def test_a_copy_succeeds_when_its_source_cannot_be_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "staged.tmp"
    source.write_bytes(b"owner writing")
    target = tmp_path / "export.txt"
    monkeypatch.setattr(files, "_rename_exclusively", unsupported)
    monkeypatch.setattr(os, "unlink", locked(lambda name: name == "staged.tmp"))

    publish_exclusively(str(source), target)

    assert target.read_bytes() == b"owner writing"


def test_a_copied_export_cleans_up_its_temporary_file_when_the_first_delete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files, "_rename_exclusively", unsupported)
    monkeypatch.setattr(os, "unlink", locked(lambda name: name.startswith(".mailbrief-"), times=1))
    target = tmp_path / "draft.txt"

    write_text_atomically(target, "owner writing", overwrite=False)

    assert target.read_text(encoding="utf-8") == "owner writing"
    assert list(tmp_path.iterdir()) == [target]


def test_a_copied_export_is_saved_even_if_its_temporary_file_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files, "_rename_exclusively", unsupported)
    monkeypatch.setattr(os, "unlink", locked(lambda name: name.startswith(".mailbrief-")))
    target = tmp_path / "draft.txt"

    write_text_atomically(target, "owner writing", overwrite=False)

    assert target.read_text(encoding="utf-8") == "owner writing"
    # Only the hidden temporary file, holding the same text, is left beside it.
    assert [
        path.name.startswith(".mailbrief-") for path in tmp_path.iterdir() if path != target
    ] == [True]
