"""Shared pytest fixtures.

Fakes/stubs live only here, per CONTRIBUTING.md #5, and are named
Fake*/Stub*.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orchestrator.main import create_app


@pytest_asyncio.fixture
async def app_client() -> AsyncGenerator[AsyncClient, None]:
    """Real FastAPI app wired to httpx's ASGI transport (no real network).

    The database itself is whatever DATABASE_URL points to for this test
    run (default: unreachable localhost, giving a genuine db-down path).
    No mocking of the health check's DB call happens here — the degrade
    path is exercised by pointing at a real-but-unreachable database, not
    by faking the result.
    """
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture(autouse=True)
def _reset_engine_state() -> None:
    """Ensure each test starts with a fresh cached engine/settings.

    Settings/engine are cached at module scope in orchestrator.core; tests
    that set DATABASE_URL via monkeypatch need a clean slate.
    """
    from orchestrator.core import db as db_module
    from orchestrator.core.config import get_settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None
    yield
    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None
