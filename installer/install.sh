#!/usr/bin/env bash
# gpu-orchestrator agent installer (M7).
#
# Usage:
#   curl -sSL http://<orchestrator-host>:<port>/install.sh | bash -s -- \
#       --token <ENROLLMENT_TOKEN> \
#       [--orchestrator http://<orchestrator-host>:<port>] \
#       [--state-dir <dir>]
#
# This script is served publicly, without auth, by the orchestrator itself
# (GET /install.sh) — the enrollment TOKEN is the secret here, not the script
# text. It is honest by construction: every prerequisite check either passes
# for real or the script prints a plain-language reason and exits non-zero.
# It never continues past a missing prerequisite and never claims success it
# did not actually observe.
#
# Targets Linux and macOS. This is a dev-phase project with peers expected to
# be Linux/macOS boxes (or friends' laptops over Tailscale later); Windows is
# not supported directly by this script. If you're on Windows, install WSL2
# (https://learn.microsoft.com/windows/wsl/install), open a WSL2 Ubuntu shell,
# and run this exact command there.

set -u
set -o pipefail

# Replaced by the orchestrator at serve time with the address this script was
# actually downloaded from, so a peer never has to be told the URL separately.
# The literal placeholder only survives when run straight from a checkout.
ORCHESTRATOR="__ORCHESTRATOR_URL__"
case "$ORCHESTRATOR" in
    __*) ORCHESTRATOR="http://localhost:8090" ;;
esac
TOKEN=""
STATE_DIR="${HOME:-$PWD}/.gpu-orchestrator-agent"
WORKDIR="${HOME:-$PWD}/.gpu-orchestrator-agent-src"

log()  { printf '[install] %s\n' "$*"; }
fail() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

# --- Parse args ---------------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --token|--orchestrator|--state-dir)
            [ $# -ge 2 ] || fail "missing value for $1"
            ;;
    esac
    case "$1" in
        --token)
            TOKEN="$2"
            shift 2
            ;;
        --orchestrator)
            ORCHESTRATOR="$2"
            shift 2
            ;;
        --state-dir)
            STATE_DIR="$2"
            shift 2
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

if [ -z "$TOKEN" ]; then
    fail "missing required --token <ENROLLMENT_TOKEN>. Mint one from the dashboard's " \
         "\"Add a node\" dialog (Overview page) and re-run: bash -s -- --token <TOKEN>"
fi
ORCHESTRATOR="${ORCHESTRATOR%/}"

log "orchestrator = $ORCHESTRATOR"

# --- OS / arch detection -------------------------------------------------------

OS_NAME="$(uname -s 2>/dev/null || echo unknown)"
ARCH_NAME="$(uname -m 2>/dev/null || echo unknown)"

case "$OS_NAME" in
    Linux|Darwin)
        log "detected OS: $OS_NAME ($ARCH_NAME)"
        ;;
    *)
        fail "this installer targets Linux and macOS; detected '$OS_NAME'. " \
             "On Windows, install WSL2 (https://learn.microsoft.com/windows/wsl/install), " \
             "open a WSL2 Ubuntu shell, and run this exact command there."
        ;;
esac

# --- Prerequisite: Python 3.11+ -------------------------------------------------

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    fail "Python 3.11+ was not found on PATH. Install Python 3.11 or newer " \
         "(https://www.python.org/downloads/) and re-run this script."
fi

PY_OK="$("$PYTHON_BIN" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)' 2>/dev/null || echo 0)"
if [ "$PY_OK" != "1" ]; then
    PY_VER="$("$PYTHON_BIN" -c 'import platform; print(platform.python_version())' 2>/dev/null || echo unknown)"
    fail "found $PYTHON_BIN but it is version $PY_VER; this project requires Python 3.11+. " \
         "Install a newer Python and re-run this script."
fi
log "Python OK: $PYTHON_BIN ($("$PYTHON_BIN" -c 'import platform; print(platform.python_version())'))"

# --- Prerequisite: Docker --------------------------------------------------

if ! command -v docker >/dev/null 2>&1; then
    fail "Docker was not found on PATH. This node executes training jobs inside " \
         "Docker containers (ADR-007 isolation), so Docker is required, not optional. " \
         "Install Docker (https://docs.docker.com/get-docker/) and re-run this script."
fi
if ! docker info >/dev/null 2>&1; then
    fail "Docker is installed but the daemon is not reachable (is it running? do you have " \
         "permission to talk to it — e.g. are you in the 'docker' group?). " \
         "Start Docker and re-run this script."
fi
log "Docker OK: $(docker --version)"

# --- Fetch a real reachability check on the orchestrator before anything else --

if command -v curl >/dev/null 2>&1; then
    HEALTH_FETCH() { curl -sf "$1"; }
elif command -v wget >/dev/null 2>&1; then
    HEALTH_FETCH() { wget -qO- "$1"; }
else
    fail "neither curl nor wget is available; install one and re-run this script."
fi

if ! HEALTH_FETCH "$ORCHESTRATOR/health" >/dev/null; then
    fail "could not reach $ORCHESTRATOR/health. Check the orchestrator address and " \
         "that this machine can reach it over the network, then re-run this script."
fi
log "orchestrator reachable at $ORCHESTRATOR"

# --- Download the agent source (this repo has no public git remote to clone; --
# --- the orchestrator packages its own currently-running agent/ on demand)   --

mkdir -p "$WORKDIR" || fail "could not create working directory $WORKDIR"
BUNDLE="$WORKDIR/agent-bundle.tar.gz"

log "downloading agent bundle from $ORCHESTRATOR/agent-bundle.tar.gz"
if command -v curl >/dev/null 2>&1; then
    curl -sSL -f "$ORCHESTRATOR/agent-bundle.tar.gz" -o "$BUNDLE" \
        || fail "failed to download the agent bundle from $ORCHESTRATOR"
else
    wget -q "$ORCHESTRATOR/agent-bundle.tar.gz" -O "$BUNDLE" \
        || fail "failed to download the agent bundle from $ORCHESTRATOR"
fi

log "extracting agent bundle into $WORKDIR"
tar -xzf "$BUNDLE" -C "$WORKDIR" || fail "failed to extract the agent bundle"

# --- Install the agent + its real runtime dependencies -------------------------

log "creating an isolated virtualenv at $WORKDIR/.venv"
"$PYTHON_BIN" -m venv "$WORKDIR/.venv" || fail "failed to create the agent virtualenv"
VENV_PY="$WORKDIR/.venv/bin/python"
[ -x "$VENV_PY" ] || fail "virtualenv creation did not produce $VENV_PY"

log "installing agent dependencies (this can take a minute)"
"$VENV_PY" -m pip install --quiet --upgrade pip \
    || fail "failed to upgrade pip in the agent virtualenv"
"$VENV_PY" -m pip install --quiet "$WORKDIR[agent]" \
    || fail "failed to install the agent's dependencies. Check network access to PyPI " \
         "and re-run this script."

log "agent installed."

# --- Enroll and run --------------------------------------------------------

log "starting the agent (enrolling with the supplied token, then heartbeating forever)"
log "state directory: $STATE_DIR"
log "press Ctrl+C to stop sharing this GPU."

exec "$VENV_PY" -m agent \
    --orchestrator "$ORCHESTRATOR" \
    --enrollment-token "$TOKEN" \
    --state-dir "$STATE_DIR"
