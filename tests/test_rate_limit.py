"""Rate limiting on credential endpoints (ADR-012 §7).

The limiter unit tests drive the clock explicitly rather than sleeping, so
window expiry is verified deterministically instead of by wall time.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestrator.api.deps import reset_rate_limiters
from orchestrator.services.ratelimit import FixedWindowRateLimiter
from tests.helpers import TEST_OPERATOR_USERNAME


def test_allows_up_to_the_limit_then_rejects() -> None:
    limiter = FixedWindowRateLimiter(limit=3, window_seconds=60)
    assert [limiter.check("ip", now=0.0).allowed for _ in range(3)] == [True] * 3
    assert limiter.check("ip", now=0.0).allowed is False


def test_rejection_reports_a_usable_retry_after() -> None:
    """A client that honours Retry-After must land *after* the window, not
    inside it — so the value is rounded up and never 0."""
    limiter = FixedWindowRateLimiter(limit=1, window_seconds=60)
    limiter.check("ip", now=0.0)
    decision = limiter.check("ip", now=59.5)
    assert decision.allowed is False
    assert decision.retry_after_seconds >= 1
    assert limiter.check("ip", now=decision.retry_after_seconds + 59.5).allowed is True


def test_window_resets_after_it_elapses() -> None:
    limiter = FixedWindowRateLimiter(limit=2, window_seconds=10)
    assert limiter.check("ip", now=0.0).allowed is True
    assert limiter.check("ip", now=1.0).allowed is True
    assert limiter.check("ip", now=2.0).allowed is False
    assert limiter.check("ip", now=10.0).allowed is True


def test_keys_are_independent() -> None:
    """One noisy client must not lock everyone else out."""
    limiter = FixedWindowRateLimiter(limit=1, window_seconds=60)
    assert limiter.check("a", now=0.0).allowed is True
    assert limiter.check("a", now=0.0).allowed is False
    assert limiter.check("b", now=0.0).allowed is True


def test_prune_drops_only_elapsed_windows() -> None:
    """Without pruning the map grows one entry per distinct client IP forever,
    which on a reachable port is an unbounded-memory path."""
    limiter = FixedWindowRateLimiter(limit=5, window_seconds=10)
    limiter.check("old", now=0.0)
    limiter.check("fresh", now=9.0)
    assert limiter.prune(now=11.0) == 1
    # The still-live window kept its count rather than being silently reset.
    assert limiter.check("fresh", now=11.0).allowed is True
    assert limiter._windows["fresh"][1] == 2  # noqa: SLF001 - asserting the invariant


@pytest.mark.parametrize("bad", [{"limit": 0}, {"window_seconds": 0}])
def test_nonsense_configuration_is_rejected_at_construction(bad: dict) -> None:  # type: ignore[type-arg]
    kwargs = {"limit": 5, "window_seconds": 60, **bad}
    with pytest.raises(ValueError):
        FixedWindowRateLimiter(**kwargs)  # type: ignore[arg-type]


# --- Wired into the real endpoints -------------------------------------------


@pytest.mark.asyncio
async def test_login_brute_force_is_throttled(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated failed logins get 429 with Retry-After, not unlimited guesses.

    Configured down to 3 attempts for the test so this doesn't depend on the
    shipped default staying at 10.
    """
    monkeypatch.setenv("LOGIN_RATE_LIMIT_ATTEMPTS", "3")
    from orchestrator.core.config import get_settings

    get_settings.cache_clear()
    reset_rate_limiters()

    body = {"username": TEST_OPERATOR_USERNAME, "password": "wrong-password"}
    codes = [
        (await anon_client.post("/auth/login", json=body)).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [401, 401, 401]
    assert 429 in codes[3:]

    limited = await anon_client.post("/auth/login", json=body)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1

    get_settings.cache_clear()
    reset_rate_limiters()


@pytest.mark.asyncio
async def test_rate_limit_does_not_leak_across_tests(
    anon_client: AsyncClient,
) -> None:
    """The limiter is process-global state; the fixture must reset it or a
    test that deliberately trips the limit poisons whichever test runs next."""
    resp = await anon_client.post(
        "/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": "wrong-password"},
    )
    assert resp.status_code == 401, "previous test's 429 window leaked"
