"""Tests for the public bootstrap surface (M7): GET /install.sh and
GET /agent-bundle.tar.gz. Both are unauthenticated by design (see
orchestrator/api/installer.py's module docstring)."""

from __future__ import annotations

import io
import tarfile

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_install_script_is_served_as_plain_text(app_client: AsyncClient) -> None:
    resp = await app_client.get("/install.sh")

    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert body.startswith("#!/usr/bin/env bash")
    assert "--token" in body
    assert "agent-bundle.tar.gz" in body
    # Honesty checks: it must actually verify prerequisites, not skip them.
    assert "Docker" in body
    assert "Python 3.11" in body
    assert "WSL2" in body


@pytest.mark.asyncio
async def test_install_script_requires_no_auth(app_client: AsyncClient) -> None:
    """Deliberately no X-Admin-Key or bearer token — this route must not gate
    on either, per its module docstring."""
    resp = await app_client.get("/install.sh")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_agent_bundle_is_a_valid_gzip_tarball_with_expected_contents(
    app_client: AsyncClient,
) -> None:
    resp = await app_client.get("/agent-bundle.tar.gz")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"

    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        names = tar.getnames()

    assert "pyproject.toml" in names
    assert any(n == "agent/main.py" or n.endswith("/agent/main.py") for n in names)
    assert any(n == "agent/__init__.py" or n.endswith("/agent/__init__.py") for n in names)
    # trainer/train.py must travel with the bundle (ADR-007 addendum): a peer
    # running unsandboxed executes it directly, and this is the only way it
    # gets the file. Shipping without it produced a bundle that installed and
    # enrolled cleanly, then failed at the first claimed lease — on somebody
    # else's laptop, which is the worst place to discover it.
    assert any(n == "trainer/train.py" or n.endswith("/trainer/train.py") for n in names)


@pytest.mark.asyncio
@pytest.mark.parametrize("script", ["/install.sh", "/install.ps1"])
async def test_the_installer_is_told_where_it_was_downloaded_from(
    app_client: AsyncClient, script: str
) -> None:
    """A peer must dial the address it fetched the installer from.

    This is the bug that made the first real peer test fail: the script carried
    a hardcoded localhost default and the one-liner never overrode it, so the
    friend's laptop tried to reach *its own* localhost. The orchestrator now
    substitutes the request's own host, so the common case needs no URL at all.
    """
    resp = await app_client.get(script, headers={"host": "peer-facing.example:9999"})

    assert resp.status_code == 200
    body = resp.text
    assert "__ORCHESTRATOR_URL__" not in body, "the placeholder must be substituted"
    assert "peer-facing.example:9999" in body


@pytest.mark.asyncio
async def test_a_tls_terminating_tunnel_yields_an_https_url(
    app_client: AsyncClient,
) -> None:
    """Behind a tunnel the orchestrator sees plain HTTP, so using its own scheme
    would hand the peer an http:// URL for an https:// endpoint."""
    resp = await app_client.get(
        "/install.ps1",
        headers={
            "host": "internal:8000",
            "x-forwarded-host": "abc.trycloudflare.com",
            "x-forwarded-proto": "https",
        },
    )

    assert resp.status_code == 200
    assert "https://abc.trycloudflare.com" in resp.text
    assert "internal:8000" not in resp.text


@pytest.mark.asyncio
async def test_the_installer_carries_this_deployment_s_trainer_image(
    app_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer must launch the image this orchestrator is configured for.

    Told out of band, the fleet drifts onto mixed images and nobody notices;
    served from config, a published reference reaches every peer automatically
    the moment the operator sets TRAINER_IMAGE.
    """
    from orchestrator.core.config import get_settings

    monkeypatch.setenv("TRAINER_IMAGE", "someuser/gpu-orchestrator-trainer:v2")
    get_settings.cache_clear()
    try:
        body = (await app_client.get("/install.ps1")).text
        assert "__TRAINER_IMAGE__" not in body, "the placeholder must be substituted"
        assert "someuser/gpu-orchestrator-trainer:v2" in body
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_install_ps1_never_calls_exit_at_top_level(
    app_client: AsyncClient,
) -> None:
    """`irm | iex` runs in the caller's session, where a bare `exit` terminates
    the PowerShell host — closing the window and destroying the error message
    with it. A peer hit exactly that: the terminal vanished and they had no idea
    why. Every failure path must `return` from the wrapper function instead.
    """
    body = (await app_client.get("/install.ps1")).text

    offenders = [
        line.strip()
        for line in body.splitlines()
        # Only flag real statements: skip comments and anything inside a quoted
        # help string (those are matched loosely, so require a line that starts
        # with the keyword).
        if line.strip().startswith("exit ") or line.strip() == "exit"
    ]
    assert offenders == [], (
        f"install.ps1 must never `exit` — it closes the user's terminal when "
        f"run via `irm | iex`. Found: {offenders}"
    )


@pytest.mark.asyncio
async def test_install_ps1_is_served_for_windows_peers(app_client: AsyncClient) -> None:
    """The bash installer needs WSL2 on Windows, which is where most volunteers
    give up. This one runs in the PowerShell they already have."""
    resp = await app_client.get("/install.ps1")

    assert resp.status_code == 200
    body = resp.text
    assert "agent-bundle.tar.gz" in body
    # It must offer the unsandboxed path explicitly rather than silently
    # choosing it, and must not require WSL2.
    assert "--allow-unsandboxed" in body
    assert "WITHOUT container isolation" in body


# --- Self-update (agent/updates.py) -------------------------------------------


@pytest.mark.asyncio
async def test_bundle_carries_the_version_the_endpoint_reports(
    app_client: AsyncClient,
) -> None:
    """The installer hands BUNDLE_VERSION to the agent as --bundle-version, and
    the agent compares it with /agent-bundle/version. If the two ever disagreed
    for the same code, every agent would restart for an update forever."""
    version = (await app_client.get("/agent-bundle/version")).json()["version"]
    resp = await app_client.get("/agent-bundle.tar.gz")
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        member = tar.extractfile("BUNDLE_VERSION")
        assert member is not None
        assert member.read().decode() == version
    assert len(version) == 16


@pytest.mark.asyncio
async def test_version_is_stable_across_requests(app_client: AsyncClient) -> None:
    """A hash of the tarball would change per request (tar and gzip embed
    timestamps); the version must not."""
    first = (await app_client.get("/agent-bundle/version")).json()["version"]
    second = (await app_client.get("/agent-bundle/version")).json()["version"]
    assert first == second


@pytest.mark.asyncio
async def test_both_installers_run_the_update_loop(app_client: AsyncClient) -> None:
    for path in ("/install.sh", "/install.ps1"):
        body = (await app_client.get(path)).text
        assert "--bundle-version" in body, path
        assert "75" in body, path
        assert "--trainer-image" in body, path
