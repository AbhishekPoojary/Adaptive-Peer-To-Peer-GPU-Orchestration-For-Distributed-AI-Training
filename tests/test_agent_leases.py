"""Pure unit tests for the agent-side lease hold/renew helpers (no network)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from agent.leases import RELEASE_REASON, held_from_response


def _lease(expires_in_s: float) -> dict[str, object]:
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "job_id": "22222222-2222-2222-2222-222222222222",
        "lease_epoch": 1,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in_s)).isoformat(),
    }


def test_held_from_response_parses_fields() -> None:
    held = held_from_response(_lease(30))
    assert held.lease_epoch == 1
    assert held.job_id == "22222222-2222-2222-2222-222222222222"
    assert held.expires_at_ts > time.time()


def test_renewal_due_true_when_within_margin() -> None:
    held = held_from_response(_lease(3))
    assert held.renewal_due(margin_seconds=5.0) is True


def test_renewal_not_due_with_plenty_of_ttl() -> None:
    held = held_from_response(_lease(60))
    assert held.renewal_due(margin_seconds=5.0) is False


def test_seconds_held_increases() -> None:
    held = held_from_response(_lease(30))
    assert held.seconds_held(now=held.held_since_ts + 2.0) == 2.0


def test_release_reason_is_honest() -> None:
    assert RELEASE_REASON == "execution-not-implemented"
