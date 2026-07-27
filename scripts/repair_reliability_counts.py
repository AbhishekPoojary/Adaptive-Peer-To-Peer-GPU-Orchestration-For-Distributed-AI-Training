"""One-off, auditable repair of reliability data recorded before revision 0007.

Why this exists
---------------
Before ``0007_unclaimed_lease_state`` the TTL sweep collapsed two different
events into ``EXPIRED``:

* an **ACTIVE** lease that passed its TTL — the node took work on and then
  stopped making progress. A real reliability signal (ADR-009).
* a **PENDING** cohort slot that was never claimed — the *scheduler* offered
  work that nobody picked up. Not evidence the node is unreliable.

Both incremented ``nodes.lease_failure_count``, so a healthy node accumulated
failures for work it was never actually given — violating CONTRIBUTING.md rule
4 ("reliability must be earned"). Revision 0007 gives the second event its own
``UNCLAIMED`` terminal state; this script reclassifies the rows that were
already written under the old behaviour and rebuilds the node counters.

Nothing here is invented. The reclassification uses the authoritative record of
whether a lease was ever actually claimed: a lease transitions to ACTIVE only
via the claim endpoint, which writes a ``JobEvent`` with ``to_state='LEASED'``
carrying that lease's id. An ``EXPIRED`` lease with no such event was never
claimed. Counters are then recomputed by counting real ``leases`` rows under
the same classification the live scheduler uses
(``services.scheduling._SUCCESS_STATES`` / ``_FAILURE_STATES``), so the flat
display counters and the adaptive scheduler's R_i input agree.

Usage
-----
Dry run (default — reports what would change, writes nothing)::

    python -m scripts.repair_reliability_counts

Apply::

    python -m scripts.repair_reliability_counts --apply

Idempotent: re-running after an apply reports zero changes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from orchestrator.core.config import get_settings
from orchestrator.models.job import JobEvent, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node
from orchestrator.services.scheduling import _FAILURE_STATES, _SUCCESS_STATES


async def _claimed_lease_ids(session: async_sessionmaker) -> set[str]:
    """Ids of every lease that was genuinely claimed, from the JobEvent log.

    A ``LEASED`` transition is written only by the claim path, and carries the
    claimed lease's id in ``detail->>'lease_id'``.
    """
    rows = (
        await session.execute(
            select(JobEvent.detail["lease_id"].astext).where(
                JobEvent.to_state == JobState.LEASED
            )
        )
    ).scalars().all()
    return {r for r in rows if r}


async def main(apply: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with sessionmaker() as session:
        # 1. Reclassify EXPIRED-but-never-claimed leases as UNCLAIMED.
        claimed = await _claimed_lease_ids(session)
        expired = (
            await session.execute(
                select(Lease).where(Lease.state == LeaseState.EXPIRED)
            )
        ).scalars().all()
        stale = [lease for lease in expired if str(lease.id) not in claimed]

        print(
            f"expired leases: {len(expired)} | never claimed (-> UNCLAIMED): "
            f"{len(stale)} | genuine active timeouts (kept EXPIRED): "
            f"{len(expired) - len(stale)}"
        )

        if apply and stale:
            # Bulk UPDATE by id; the enum label exists as of revision 0007.
            await session.execute(
                text(
                    "UPDATE leases SET state = 'UNCLAIMED' "
                    "WHERE id = ANY(:ids)"
                ).bindparams(ids=[lease.id for lease in stale])
            )
            await session.flush()

        # 2. Recompute node counters from real lease rows, using exactly the
        #    classification the live scheduler applies.
        counts = {
            (node_id, state): n
            for node_id, state, n in (
                await session.execute(
                    select(Lease.node_id, Lease.state, func.count())
                    .group_by(Lease.node_id, Lease.state)
                )
            ).all()
        }
        # In a dry run the UPDATE above did not execute, so the tally still sees
        # the never-claimed rows as EXPIRED. Subtract them so the report shows
        # the counters the apply path would actually produce.
        if not apply:
            for lease in stale:
                key = (lease.node_id, LeaseState.EXPIRED)
                if counts.get(key):
                    counts[key] -= 1
        nodes = (await session.execute(select(Node))).scalars().all()

        print(f"\n{'node':<10} {'ok':>10} {'fail':>12}")
        changed = 0
        for node in sorted(nodes, key=lambda n: n.name):
            ok = sum(counts.get((node.id, s), 0) for s in _SUCCESS_STATES)
            fail = sum(counts.get((node.id, s), 0) for s in _FAILURE_STATES)
            was = (node.lease_success_count, node.lease_failure_count)
            if was != (ok, fail):
                changed += 1
                print(f"{node.name:<10} {was[0]:>4} -> {ok:<4} {was[1]:>6} -> {fail:<4}")
                if apply:
                    node.lease_success_count = ok
                    node.lease_failure_count = fail
            else:
                print(f"{node.name:<10} {ok:>10} {fail:>12}   (unchanged)")

        if apply:
            await session.commit()
            print(f"\nAPPLIED: {len(stale)} lease(s) reclassified, {changed} node(s) updated.")
        else:
            print(
                f"\nDRY RUN: would reclassify {len(stale)} lease(s) and update "
                f"{changed} node(s). Re-run with --apply to write."
            )

    await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes. Without this flag the script only reports.",
    )
    sys.exit(asyncio.run(main(parser.parse_args().apply)))
