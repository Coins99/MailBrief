"""Global pytest configuration and environment hooks."""

import os
import socket
from typing import Any

import pytest

# Ensure Qt runs in offscreen mode in headless test environments
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

NETWORK_BLOCKED = "Tests must not use the network."
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "", None})
_LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})


def _check_destination(sock: socket.socket, address: Any) -> None:
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return
    host = address[0] if isinstance(address, tuple) and address else None
    if host not in _LOOPBACK_ADDRESSES:
        raise RuntimeError(NETWORK_BLOCKED)


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any non-loopback lookup or connection.

    A missing HTTP mock must never reach real mail or AI services. On Windows, asyncio's
    proactor bypasses socket.connect; the getaddrinfo guard still covers hostnames there.
    """
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host: bytes | str | None, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else host
        if name not in _LOOPBACK_NAMES:
            raise RuntimeError(NETWORK_BLOCKED)
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self: socket.socket, address: Any) -> None:
        _check_destination(self, address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _check_destination(self, address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
