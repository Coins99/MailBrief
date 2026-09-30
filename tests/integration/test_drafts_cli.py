"""mailbrief-gmail-diagnostic drafts: list and export, with no Gmail or AI access."""

import asyncio
import re
from pathlib import Path
from typing import Any, NoReturn

import pytest

from mailbrief.diagnostics import gmail
from mailbrief.domain.drafts import DraftEdit, DraftKind
from mailbrief.services.drafts import DraftService
from mailbrief.storage.database import Database
from mailbrief.storage.migrate import upgrade_database


def refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise AssertionError("The drafts commands must never open Gmail or Groq.")


@pytest.fixture(autouse=True)
def no_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gmail, "gmail_provider", refuse)
    monkeypatch.setattr(gmail, "groq_provider", refuse)


def seed(path: Path) -> list[str]:
    """A reply with a placeholder and a note, on a migrated database; returns their IDs."""
    upgrade_database(path)

    async def create() -> list[str]:
        database = Database.from_path(path)
        try:
            async with database.session() as session:
                service = DraftService(session)
                reply = await service.create(DraftKind.EMAIL)
                await service.autosave(
                    reply.public_id,
                    1,
                    DraftEdit(
                        title="Budget \x1b[31mupdate",
                        to_text="alex@example.com",
                        body="Hi [[name]],\n\tsee the numbers.\x07\n",
                    ),
                )
                note = await service.create(DraftKind.NOTE)
                await service.autosave(note.public_id, 1, DraftEdit(title="Plan", body="Ideas"))
                return [reply.public_id, note.public_id]
        finally:
            await database.dispose()

    return asyncio.run(create())


def run(path: Path, *options: str) -> int:
    return gmail.main(["drafts", *options, "--database", str(path)])


def test_list_shows_ids_kinds_titles_times_and_placeholders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "drafts.sqlite3"
    reply, note = seed(path)

    assert run(path, "list") == 0

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert re.fullmatch(rf"{note} \[note\] Plan; updated \S+; placeholders 0", lines[0])
    # Terminal control characters from the title are never printed.
    assert re.fullmatch(
        rf"{reply} \[email\] Budget \[31mupdate; updated \S+; placeholders 1", lines[1]
    )


def test_an_empty_list_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "empty.sqlite3"

    assert run(path, "list") == 0

    assert capsys.readouterr().out == "No drafts.\n"


def test_export_prints_text_or_markdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "drafts.sqlite3"
    reply, note = seed(path)

    assert run(path, "export", reply) == 0
    assert capsys.readouterr().out == (
        "To: alex@example.com\nSubject: Budget [31mupdate\n\nHi [[name]],\n\tsee the numbers.\n"
    )
    assert run(path, "export", note, "--format", "markdown") == 0
    assert capsys.readouterr().out == "# Plan\n\nIdeas\n"


def test_export_to_a_new_file_writes_the_exact_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "drafts.sqlite3"
    reply, _ = seed(path)
    out = tmp_path / "reply.md"

    assert run(path, "export", reply, "--format", "markdown", "--out", str(out)) == 0

    assert capsys.readouterr().out == f"Exported to {out}. 1 placeholder still needs filling.\n"
    assert out.read_bytes() == (
        b"**To:** alex@example.com  \n**Subject:** Budget \x1b[31mupdate\n\n"
        b"Hi [[name]],\n\tsee the numbers.\x07\n"
    )


def test_export_never_overwrites_an_existing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "drafts.sqlite3"
    _, note = seed(path)
    out = tmp_path / "note.txt"
    out.write_text("mine", encoding="utf-8")

    assert run(path, "export", note, "--out", str(out)) == 3

    assert capsys.readouterr().out == (
        "That file already exists; nothing was written. Choose another --out path.\n"
    )
    assert out.read_text(encoding="utf-8") == "mine"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["drafts.sqlite3", "note.txt"]


def test_an_unknown_draft_id_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "drafts.sqlite3"
    seed(path)

    assert run(path, "export", "00000000-0000-4000-8000-000000000999") == 3

    assert capsys.readouterr().out == "That draft was not found.\n"


def test_a_missing_output_folder_is_a_file_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "drafts.sqlite3"
    _, note = seed(path)

    assert run(path, "export", note, "--out", str(tmp_path / "missing" / "note.txt")) == 5

    assert "Local database or file operation failed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("action", "documented"),
    [("list", ("--database",)), ("export", ("--format", "--out", "--database"))],
)
def test_drafts_help(
    action: str, documented: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        gmail.main(["drafts", action, "--help"])

    assert exited.value.code == 0
    shown = capsys.readouterr().out
    for option in documented:
        assert option in shown
