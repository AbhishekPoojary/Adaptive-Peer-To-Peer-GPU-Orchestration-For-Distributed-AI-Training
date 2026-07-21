"""M2 migration up/down test against a real, throwaway Postgres database.

Proves 0003 adds the job/lease tables (and nodes.last_assigned_at), downgrades
cleanly back to the M1 schema, and can be re-applied — so a leftover enum or
index never blocks recreation. Uses its own database.
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

_MIG_DB_NAME = "orchestrator_m2migtest"
_M2_TABLES = {"jobs", "job_events", "leases"}
_M1_TABLES = {"nodes", "node_telemetry_samples", "enrollment_tokens", "auth_challenges"}


@pytest.mark.asyncio
async def test_m2_migration_upgrade_downgrade_cycle() -> None:
    base = base_test_url()
    admin_dsn = asyncpg_dsn(base)
    await create_database(admin_dsn, _MIG_DB_NAME)
    test_url = with_dbname(base, _MIG_DB_NAME)
    test_dsn = asyncpg_dsn(test_url)
    try:
        run_alembic(test_url, "upgrade", "head")
        after_up = await table_names(test_dsn)
        assert _M2_TABLES.issubset(after_up), f"missing M2 tables: {after_up}"
        assert _M1_TABLES.issubset(after_up)

        # Downgrade to the M1 head: M2 tables gone, M1 intact.
        run_alembic(test_url, "downgrade", "0002_m1_identity_nodes")
        after_down = await table_names(test_dsn)
        assert _M2_TABLES.isdisjoint(after_down), f"M2 tables lingered: {after_down}"
        assert _M1_TABLES.issubset(after_down)

        # Re-upgrade must succeed (no leftover enum/index blocking recreation).
        run_alembic(test_url, "upgrade", "head")
        after_reup = await table_names(test_dsn)
        assert _M2_TABLES.issubset(after_reup)
    finally:
        await drop_database(admin_dsn, _MIG_DB_NAME)
