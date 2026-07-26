"""Public, unauthenticated bootstrap surface for a new peer node (M7).

``GET /install.sh`` serves the real bootstrap script verbatim as plain text —
the TOKEN passed to it is the secret, not the script, so no auth gates this
route (mirroring how curl-pipe-to-bash installers are conventionally served).

``GET /agent-bundle.tar.gz`` packages the orchestrator's own currently-running
``agent/`` package + a trimmed ``pyproject.toml`` on the fly, straight from the
files on disk inside this container. This project has no public git remote to
clone from (dev-phase, single-operator repo), so "download the agent" means
"download it from the orchestrator that is bootstrapping you" rather than
assuming GitHub reachability — the bundle is always exactly the code this
orchestrator is running, never a stale or separately-versioned copy.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse, Response

router = APIRouter()

# orchestrator/api/installer.py -> orchestrator/api -> orchestrator -> repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_INSTALL_SCRIPT = _REPO_ROOT / "installer" / "install.sh"
_AGENT_DIR = _REPO_ROOT / "agent"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_README = _REPO_ROOT / "README.md"


@router.get("/install.sh", response_class=PlainTextResponse)
async def get_install_script() -> str:
    """Serve the real bootstrap script. 404s honestly if it isn't present on
    this deployment rather than fabricating a body."""
    if not _INSTALL_SCRIPT.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="install.sh is not available"
        )
    return _INSTALL_SCRIPT.read_text(encoding="utf-8")


def _exclude_pycache(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Drop __pycache__/*.pyc noise so the bundle is exactly the source, not
    whatever bytecode this container happened to compile."""
    if "__pycache__" in tarinfo.name or tarinfo.name.endswith((".pyc", ".pyo")):
        return None
    return tarinfo


@router.get("/agent-bundle.tar.gz")
async def get_agent_bundle() -> Response:
    """Package the live ``agent/`` source + ``pyproject.toml`` this process is
    actually running into a gzipped tarball, built in memory per request (the
    agent package is a few dozen small .py files — negligible cost)."""
    if not _AGENT_DIR.is_dir() or not _PYPROJECT.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent bundle is not available on this deployment",
        )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(_AGENT_DIR, arcname="agent", filter=_exclude_pycache)
        tar.add(_PYPROJECT, arcname="pyproject.toml")
        # pyproject.toml declares readme = "README.md"; setuptools reads that
        # file at build/metadata time, so it must travel with the bundle or a
        # bare `pip install .` of the extracted tarball fails to build.
        if _README.is_file():
            tar.add(_README, arcname="README.md")
    return Response(content=buf.getvalue(), media_type="application/gzip")
