from __future__ import annotations

import pytest

from helpers import CFG, FakePortal

from b24_mcp import client as client_mod


@pytest.fixture
def portal(monkeypatch):
    """Ставит FakePortal вместо urlopen; паузы между ретраями не ждём.

    config.load подменён на CFG: инструментам и фоллоу-апам .env не нужен.
    """
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr("b24_mcp.config.load", lambda: CFG)

    def install(script: dict[str, list]) -> FakePortal:
        fake = FakePortal(script)
        monkeypatch.setattr(client_mod.urllib.request, "urlopen", fake)
        return fake

    return install
