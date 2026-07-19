"""Migration up/down test against a real, throwaway Postgres database.

Proves 0002 upgrades cleanly (all M1 tables present), downgrades cleanly back
to the baseline (M1 tables gone, alembic_version still present), and can be
re-applied. Uses its own database so it never disturbs the shared test DB.
"""

from __future__ import annotations

import pytest

from tests.helpers import (
    asyncpg_dsn,
    base_test_url,
    create_database,
    drop_database,
    run_alembic,
    table_names,
    with_dbname,
)

_MIG_DB_NAME = "orchestrator_migtest"
_M1_TABLES = {
    "enrollment_tokens",
    "nodes",
    "node_telemetry_samples",
    "auth_challenges",
}


@pytest.mark.asyncio
async def test_migration_upgrade_downgrade_cycle() -> None:
    base = base_test_url()
    admin_dsn = asyncpg_dsn(base)
    await create_database(admin_dsn, _MIG_DB_NAME)
    test_url = with_dbname(base, _MIG_DB_NAME)
    test_dsn = asyncpg_dsn(test_url)
    try:
        # Upgrade to head: every M1 table must exist.
        run_alembic(test_url, "upgrade", "head")
        after_up = await table_names(test_dsn)
        assert _M1_TABLES.issubset(after_up), f"missing tables after upgrade: {after_up}"

        # Downgrade to baseline: M1 tables gone, migration chain intact.
        run_alembic(test_url, "downgrade", "0001_baseline")
        after_down = await table_names(test_dsn)
        assert _M1_TABLES.isdisjoint(after_down), f"tables lingered: {after_down}"
        assert "alembic_version" in after_down

        # Re-upgrade must succeed (no leftover enum/sequence blocking recreation).
        run_alembic(test_url, "upgrade", "head")
        after_reup = await table_names(test_dsn)
        assert _M1_TABLES.issubset(after_reup)
    finally:
        await drop_database(admin_dsn, _MIG_DB_NAME)
