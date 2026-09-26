"""The suite-wide guard: no lookups or connections beyond loopback."""

import socket
from pathlib import Path

import pytest

from tests.conftest import NETWORK_BLOCKED

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
