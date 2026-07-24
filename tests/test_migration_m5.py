"""M5 migration up/down test against a real, throwaway Postgres database.

Proves 0006 adds ``leases.rank`` and ``jobs.rendezvous_node_id`` and swaps the
per-job ACTIVE-lease unique index for the per-(job, rank) non-terminal one,
downgrades cleanly back to the M4 schema shape, and can be re-applied — so a
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
    with_dbname,
)

_MIG_DB_NAME = "orchestrator_m5migtest"


async def _columns(dsn: str, table: str) -> set[str]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
            table,
        )
    finally:
        await conn.close()
    return {r["column_name"] for r in rows}


async def _indexes(dsn: str, table: str) -> set[str]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = $1", table
        )
    finally:
        await conn.close()
    return {r["indexname"] for r in rows}


@pytest.mark.asyncio
async def test_m5_migration_upgrade_downgrade_cycle() -> None:
    base = base_test_url()
    admin_dsn = asyncpg_dsn(base)
    await create_database(admin_dsn, _MIG_DB_NAME)
    test_url = with_dbname(base, _MIG_DB_NAME)
    test_dsn = asyncpg_dsn(test_url)
    try:
        run_alembic(test_url, "upgrade", "head")
        assert "rank" in await _columns(test_dsn, "leases")
        assert "rendezvous_node_id" in await _columns(test_dsn, "jobs")
        lease_idx = await _indexes(test_dsn, "leases")
        assert "uq_active_lease_per_job_rank" in lease_idx
        assert "uq_one_active_lease_per_job" not in lease_idx

        # Downgrade to the M4 head: M5 columns gone, M2 index restored verbatim.
        run_alembic(test_url, "downgrade", "0005_m4_training_execution")
        assert "rank" not in await _columns(test_dsn, "leases")
        assert "rendezvous_node_id" not in await _columns(test_dsn, "jobs")
        lease_idx_down = await _indexes(test_dsn, "leases")
        assert "uq_one_active_lease_per_job" in lease_idx_down
        assert "uq_active_lease_per_job_rank" not in lease_idx_down

        # Re-upgrade must succeed (no leftover index blocking recreation).
        run_alembic(test_url, "upgrade", "head")
        assert "rank" in await _columns(test_dsn, "leases")
        assert "uq_active_lease_per_job_rank" in await _indexes(test_dsn, "leases")
    finally:
        await drop_database(admin_dsn, _MIG_DB_NAME)
