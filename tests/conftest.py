"""Global test safety: no real network access, ever.

Any attempt to open an INET socket (Telegram, OpenSea, CoinGecko, ...) fails
the test immediately, so every network call must be faked.
"""
from __future__ import annotations

import socket

import pytest

_real_connect = socket.socket.connect


def _guarded_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise RuntimeError(f"Network access blocked in tests: {address!r}")
    return _real_connect(self, address)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(
        socket, "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"Network access blocked in tests: {a!r}")),
    )
    yield
