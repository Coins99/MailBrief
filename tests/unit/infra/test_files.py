"""Atomic exports: UTF-8 bytes as given, no partial files, and optional overwrite refusal."""

import os
from pathlib import Path

import pytest

from mailbrief.infra.files import write_text_atomically


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
