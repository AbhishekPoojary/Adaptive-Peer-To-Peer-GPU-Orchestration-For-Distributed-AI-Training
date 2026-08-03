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
