"""Shared pytest fixtures.

Fakes/stubs live only here, per CONTRIBUTING.md #5, and are named Fake*/Stub*.

The DB-backed fixtures require a real Postgres (TEST_DATABASE_URL, e.g. the
compose stack in deploy/compose.yaml). They provision an isolated test database,
run the real Alembic migrations against it, and truncate between tests — no
SQLite substitution, because the concurrency guarantees under test are
Postgres row-locking semantics that SQLite does not share.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orchestrator.core import db as db_module
from orchestrator.core.config import get_settings
from orchestrator.core.db import dispose_engine
from orchestrator.main import create_app
from tests.helpers import (
    TEST_ADMIN_KEY,
    asyncpg_dsn,
    base_test_url,
    create_database,
    drop_database,
    truncate_all,
    with_dbname,
)

_TEST_DB_NAME = "orchestrator_m1test"


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
def _reset_engine_state() -> Iterator[None]:
    """Ensure each test starts with a fresh cached engine/settings.

    Settings/engine are cached at module scope in orchestrator.core; tests
    that set DATABASE_URL via monkeypatch need a clean slate.
    """
    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None
    yield
    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None


@pytest.fixture(scope="session")
def migrated_db_url() -> Iterator[str]:
    """Create an isolated test database, migrate it to head, drop it after."""
    from tests.helpers import run_alembic

    base = base_test_url()
    admin_dsn = asyncpg_dsn(base)
    asyncio.run(create_database(admin_dsn, _TEST_DB_NAME))
    test_url = with_dbname(base, _TEST_DB_NAME)
    run_alembic(test_url, "upgrade", "head")
    try:
        yield test_url
    finally:
        asyncio.run(drop_database(admin_dsn, _TEST_DB_NAME))


@pytest_asyncio.fixture
async def api_client(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncClient, None]:
    """FastAPI app bound to the migrated test DB, truncated before each test."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)
    monkeypatch.setenv("APP_ENV", "dev")
    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None

    await truncate_all(asyncpg_dsn(migrated_db_url))

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await dispose_engine()
