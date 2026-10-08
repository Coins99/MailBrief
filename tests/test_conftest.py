"""The suite-wide fixtures: no network beyond loopback, and no SQLite disk flushes."""

import socket
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from mailbrief.storage import database
from mailbrief.storage.backup import create_backup
from mailbrief.storage.database import Database
from mailbrief.storage.migrate import upgrade_database
from tests.conftest import NETWORK_BLOCKED, REAL_CONNECT

UNIX_FAMILY = getattr(socket, "AF_UNIX", None)  # Absent on Windows, where typeshed omits it.


def test_a_public_hostname_cannot_be_resolved() -> None:
    with pytest.raises(RuntimeError, match=NETWORK_BLOCKED):
        socket.getaddrinfo("example.com", 443)


def test_a_public_address_cannot_be_reached() -> None:
    with pytest.raises(RuntimeError, match=NETWORK_BLOCKED):
        socket.create_connection(("192.0.2.1", 443), timeout=1)


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_a_raw_socket_cannot_connect_beyond_loopback(method: str) -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(RuntimeError, match=NETWORK_BLOCKED),
    ):
        getattr(sock, method)(("192.0.2.1", 443))


def test_a_loopback_connection_still_works() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=1) as client:
            accepted, _ = server.accept()
            with accepted:
                client.sendall(b"ping")
                assert accepted.recv(4) == b"ping"


@pytest.mark.skipif(UNIX_FAMILY is None, reason="needs Unix domain sockets")
def test_other_address_families_pass_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert UNIX_FAMILY is not None
    monkeypatch.chdir(tmp_path)  # A short relative path stays within the AF_UNIX length limit.
    with socket.socket(UNIX_FAMILY, socket.SOCK_STREAM) as sock:
        assert sock.connect_ex("absent.sock") != 0


async def test_a_test_database_does_not_flush_to_disk(tmp_path: Path) -> None:
    test_database = Database.from_path(tmp_path / "mailbrief.sqlite3")
    try:
        async with test_database.engine.connect() as connection:
            synchronous = await connection.exec_driver_sql("PRAGMA synchronous")
            assert synchronous.scalar_one() == 0
    finally:
        await test_database.dispose()


def test_the_app_keeps_sqlites_default_disk_flushes(tmp_path: Path) -> None:
    with closing(REAL_CONNECT(tmp_path / "default.sqlite3")) as plain:
        (default,) = plain.execute("PRAGMA synchronous").fetchone()

    with closing(REAL_CONNECT(tmp_path / "mailbrief.sqlite3")) as connection:
        database._configure_sqlite_connection(connection, None)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA synchronous").fetchone() == (default,)
    assert default != 0


def test_migrations_and_backups_do_not_flush_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = sqlite3.connect  # The suite's wrapper.
    opened: list[int] = []

    def record(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection: sqlite3.Connection = connect(*args, **kwargs)
        opened.append(connection.execute("PRAGMA synchronous").fetchone()[0])
        return connection

    monkeypatch.setattr(sqlite3, "connect", record)
    path = tmp_path / "mailbrief.sqlite3"

    upgrade_database(path)
    upgrading = opened.copy()
    opened.clear()
    create_backup(path, tmp_path / "backup.zip")

    assert upgrading and set(upgrading) == {0}
    assert opened and set(opened) == {0}


def test_a_file_that_is_not_a_database_fails_where_the_code_reads_it(tmp_path: Path) -> None:
    path = tmp_path / "damaged.sqlite3"
    path.write_bytes(b"not a database" * 100)

    with (
        closing(sqlite3.connect(path)) as connection,
        pytest.raises(sqlite3.DatabaseError, match="not a database"),
    ):
        connection.execute("SELECT name FROM sqlite_master")
