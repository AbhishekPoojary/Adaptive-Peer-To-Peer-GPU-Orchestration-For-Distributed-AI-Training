"""Evidence that a node is alive besides its heartbeats.

The failure detector judges a node by its heartbeats alone, and a heartbeat can
be delayed by the very work the node is doing. Over a Cloudflare tunnel, a peer
downloading its dataset saturated the link, no heartbeat got through for 9 s,
and the detector declared it dead while the orchestrator was, at that moment,
serving that node its dataset chunk by chunk.

So any traffic a node genuinely drives counts as proof of life:
consuming a dataset chunk, uploading a checkpoint, a log or metric frame,
a lease renewal. Each is recorded here, and the detector will not declare a
node dead that was active within the silence floor.

In memory and per process, deliberately. The detector runs in the same process
(ADR-010 deploys one orchestrator), the record only ever needs the last few
seconds, and losing it on restart costs nothing: the next heartbeat or request
re-establishes it. A database write on every dataset chunk would turn evidence
of liveness into load.
"""

from __future__ import annotations

import time
import uuid

_last_activity: dict[uuid.UUID, float] = {}


def record_activity(node_id: uuid.UUID) -> None:
    """Note that ``node_id`` just did something only a live node could do."""
    _last_activity[node_id] = time.monotonic()


def active_within(node_id: uuid.UUID, seconds: float) -> bool:
    """Whether ``node_id`` showed activity in the last ``seconds``."""
    seen = _last_activity.get(node_id)
    return seen is not None and time.monotonic() - seen <= seconds


def forget(node_id: uuid.UUID) -> None:
    """Drop a node's record (tests; a node that has gone)."""
    _last_activity.pop(node_id, None)
