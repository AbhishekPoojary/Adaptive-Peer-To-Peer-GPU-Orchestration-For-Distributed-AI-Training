"""Keeping a peer's agent current without anyone on the peer doing anything.

The agent is installed from the bundle the orchestrator serves, and until this
existed it then ran that code forever. A fix on the orchestrator's side reached
a peer only if its owner happened to re-run the installer -- which, for a
person whose only job is to leave their GPU switched on, is never.

So the agent compares the bundle version it was installed from with the one
the orchestrator currently serves, and when they differ it exits with
:data:`UPDATE_EXIT_CODE`. The installer runs the agent in a loop, and on that
code it downloads the new bundle, reinstalls, and starts the agent again with
the same identity. The agent never replaces its own files while running:
everything it does here is decide.

It checks only while idle. A job in progress is never interrupted for an
update; the next idle check picks it up.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("agent.updates")

#: "Restart me with a newer bundle." 75 is EX_TEMPFAIL from sysexits.h: a
#: temporary condition, try again -- which is exactly what the installer does.
UPDATE_EXIT_CODE = 75


class RestartForUpdate(Exception):  # noqa: N818 - a signal, not an error
    """Raised out of the main loop to exit with :data:`UPDATE_EXIT_CODE`."""

    def __init__(self, current: str, available: str) -> None:
        super().__init__(f"bundle {current} -> {available}")
        self.current = current
        self.available = available


async def newer_bundle_version(
    client: httpx.AsyncClient, *, orchestrator: str, installed: str
) -> str | None:
    """The orchestrator's bundle version if it differs from ``installed``.

    ``None`` both when the versions match and when the check could not be made
    (an older orchestrator without the endpoint, a network blip): failing to
    learn about an update must never stop a working agent.
    """
    try:
        response = await client.get(f"{orchestrator}/agent-bundle/version")
        response.raise_for_status()
        available = str(response.json()["version"])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.debug("update check failed: %s", exc)
        return None
    return available if available and available != installed else None
