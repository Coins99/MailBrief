"""mailbrief-gmail-diagnostic actions list says when the completed list is capped."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mailbrief.diagnostics import gmail
from mailbrief.storage.database import Database
from mailbrief.storage.migrate import upgrade_database
from mailbrief.storage.tables import ActionTable

DONE = datetime(2026, 9, 28, 12, tzinfo=UTC)


def seed_completed(path: Path, count: int) -> None:
    upgrade_database(path)

    async def add() -> None:
        database = Database.from_path(path)
        try:
            async with database.session() as session:
                for index in range(count):
                    session.add(
                        ActionTable(
                            public_id=f"00000000-0000-4000-8000-{index:012d}",
                            title=f"Action {index}",
                            ownership="mine",
                            status="completed",
                            deadline_precision="none",
                            notes="",
                            created_at_utc=DONE,
                            updated_at_utc=DONE,
                            completed_at_utc=DONE + timedelta(minutes=index),
                            revision=1,
                        )
                    )
                await session.commit()
        finally:
            await database.dispose()

    asyncio.run(add())


@pytest.mark.parametrize(("count", "footer"), [(200, False), (203, True)])
def test_the_completed_list_says_when_more_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], count: int, footer: bool
) -> None:
    path = tmp_path / "actions.sqlite3"
    seed_completed(path, count)

    code = gmail.main(
        ["actions", "list", "--view", "completed", "--timezone", "UTC", "--database", str(path)]
    )

    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(f"00000000-0000-4000-8000-{count - 1:012d} [completed]")
    if footer:
        assert len(lines) == 201
        assert lines[-1] == "Showing the newest 200 of 203 completed actions."
    else:
        assert len(lines) == 200
