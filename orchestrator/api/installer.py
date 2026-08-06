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

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, Response

from orchestrator.core.config import get_settings

router = APIRouter()

# orchestrator/api/installer.py -> orchestrator/api -> orchestrator -> repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_INSTALL_SCRIPT = _REPO_ROOT / "installer" / "install.sh"
_INSTALL_SCRIPT_PS1 = _REPO_ROOT / "installer" / "install.ps1"
_AGENT_DIR = _REPO_ROOT / "agent"
_TRAINER_DIR = _REPO_ROOT / "trainer"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_README = _REPO_ROOT / "README.md"


#: Placeholder the installers carry; replaced at serve time with the address the
#: peer actually fetched from. Without this the scripts fall back to a localhost
#: default, which is correct only on the orchestrator's own machine — and is
#: silently wrong on every peer, which is the whole audience.
_ORCHESTRATOR_URL_PLACEHOLDER = "__ORCHESTRATOR_URL__"


def _public_base_url(request: Request) -> str:
    """The base URL this peer used to reach us, from the request itself.

    A peer downloads the installer from wherever the orchestrator is actually
    reachable — a LAN address, a Tailscale IP, a tunnel hostname — and that is
    exactly the address its agent must dial. Reading it back off the request
    means the operator never has to remember to pass it, and it cannot drift
    from reality.

    Honours ``X-Forwarded-Proto``/``X-Forwarded-Host`` because a tunnel or
    reverse proxy terminates TLS in front of us, so ``request.url.scheme`` would
    say "http" for a peer that really used HTTPS. These headers are only used to
    build a default the caller can override with ORCH_URL, so a spoofed value
    misconfigures the spoofer and nobody else.
    """
    forwarded_host = request.headers.get("x-forwarded-host")
    host = forwarded_host or request.headers.get("host")
    if not host:
        return "http://localhost:8090"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme}://{host}".rstrip("/")


#: Replaced at serve time with this deployment's configured trainer image, so a
#: peer is never told the name out of band and the fleet cannot end up running
#: mixed images.
_TRAINER_IMAGE_PLACEHOLDER = "__TRAINER_IMAGE__"


def _serve_script(path: Path, name: str, request: Request) -> str:
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{name} is not available"
        )
    body = path.read_text(encoding="utf-8")
    body = body.replace(_ORCHESTRATOR_URL_PLACEHOLDER, _public_base_url(request))
    return body.replace(_TRAINER_IMAGE_PLACEHOLDER, get_settings().trainer_image)


@router.get("/install.sh", response_class=PlainTextResponse)
async def get_install_script(request: Request) -> str:
    """Serve the real bootstrap script. 404s honestly if it isn't present on
    this deployment rather than fabricating a body."""
    return _serve_script(_INSTALL_SCRIPT, "install.sh", request)


@router.get("/install.ps1", response_class=PlainTextResponse)
async def get_install_script_powershell(request: Request) -> str:
    """Serve the native-Windows bootstrap script (ADR-007 addendum).

    The bash installer requires WSL2 on Windows, and GPU passthrough there needs
    the driver plus the container toolkit *inside* WSL — enough friction to lose
    most volunteers. This one runs in the PowerShell a Windows user already has.
    """
    return _serve_script(_INSTALL_SCRIPT_PS1, "install.ps1", request)


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
    # trainer/ is required, not optional: a peer running unsandboxed (ADR-007
    # addendum) executes train.py directly, and this bundle is the only way it
    # gets that file. An earlier version skipped it when absent, which shipped a
    # silently incomplete bundle — the peer installed cleanly, enrolled, claimed
    # work, and only then failed with "could not find trainer/train.py". Failing
    # here instead makes a mis-built image obvious to the operator rather than
    # to their friend.
    missing = [
        name
        for name, path in (
            ("agent/", _AGENT_DIR),
            ("trainer/", _TRAINER_DIR),
            ("pyproject.toml", _PYPROJECT),
        )
        if not (path.is_dir() or path.is_file())
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"agent bundle is incomplete on this deployment (missing: "
                f"{', '.join(missing)}). The orchestrator image must COPY them."
            ),
        )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(_AGENT_DIR, arcname="agent", filter=_exclude_pycache)
        tar.add(_TRAINER_DIR, arcname="trainer", filter=_exclude_pycache)
        tar.add(_PYPROJECT, arcname="pyproject.toml")
        # pyproject.toml declares readme = "README.md"; setuptools reads that
        # file at build/metadata time, so it must travel with the bundle or a
        # bare `pip install .` of the extracted tarball fails to build.
        if _README.is_file():
            tar.add(_README, arcname="README.md")
    return Response(content=buf.getvalue(), media_type="application/gzip")
