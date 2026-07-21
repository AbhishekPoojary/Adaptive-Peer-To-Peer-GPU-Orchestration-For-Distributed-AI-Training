"""M3 migration up/down test against a real, throwaway Postgres database.

Proves 0004 adds the scheduling-decision audit tables, downgrades cleanly back
to the M2 schema (leaving jobs/leases intact), and can be re-applied — so a
leftover index never blocks recreation. Uses its own database.
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

_MIG_DB_NAME = "orchestrator_m3migtest"
_M3_TABLES = {"scheduling_decisions", "scheduling_decision_candidates"}
_M2_TABLES = {"jobs", "job_events", "leases"}


@pytest.mark.asyncio
async def test_m3_migration_upgrade_downgrade_cycle() -> None:
    base = base_test_url()
    admin_dsn = asyncpg_dsn(base)
    await create_database(admin_dsn, _MIG_DB_NAME)
    test_url = with_dbname(base, _MIG_DB_NAME)
    test_dsn = asyncpg_dsn(test_url)
    try:
        run_alembic(test_url, "upgrade", "head")
        after_up = await table_names(test_dsn)
        assert _M3_TABLES.issubset(after_up), f"missing M3 tables: {after_up}"
        assert _M2_TABLES.issubset(after_up)

        # Downgrade to the M2 head: M3 tables gone, M2 intact.
        run_alembic(test_url, "downgrade", "0003_m2_jobs_leases")
        after_down = await table_names(test_dsn)
        assert _M3_TABLES.isdisjoint(after_down), f"M3 tables lingered: {after_down}"
        assert _M2_TABLES.issubset(after_down)

        # Re-upgrade must succeed (no leftover index blocking recreation).
        run_alembic(test_url, "upgrade", "head")
        after_reup = await table_names(test_dsn)
        assert _M3_TABLES.issubset(after_reup)
    finally:
        await drop_database(admin_dsn, _MIG_DB_NAME)
