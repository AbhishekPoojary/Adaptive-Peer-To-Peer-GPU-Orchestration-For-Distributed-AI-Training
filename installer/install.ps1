# gpu-orchestrator agent installer for Windows (native PowerShell).
#
# Usage:
#   $env:ORCH_TOKEN='<ENROLLMENT_TOKEN>'
#   irm http://<orchestrator-host>:<port>/install.ps1 | iex
#
# or, if you have the file:
#   .\install.ps1 -Token <ENROLLMENT_TOKEN> -Orchestrator http://<host>:<port>
#
# Why this exists: install.sh needs WSL2 on Windows, and GPU passthrough inside
# WSL additionally needs the NVIDIA Container Toolkit. That is enough setup to
# lose most volunteers. This script uses the PowerShell a Windows user already
# has, and — when Docker is absent — the agent's unsandboxed execution path
# (ADR-007 addendum), so the only prerequisite is Python.
#
# Honest by construction, exactly like install.sh: every prerequisite check
# either really passes or the script prints a plain-language reason and exits
# non-zero. It never continues past a missing prerequisite and never claims
# success it did not observe.

[CmdletBinding()]
param(
    [string]$Token = $env:ORCH_TOKEN,
    [string]$Orchestrator = $(if ($env:ORCH_URL) { $env:ORCH_URL } else { "http://localhost:8090" }),
    [string]$StateDir = "$env:USERPROFILE\.gpu-orchestrator-agent",
    [string]$WorkDir = "$env:USERPROFILE\.gpu-orchestrator-agent-src"
)

$ErrorActionPreference = "Stop"

function Write-Step { param($Message) Write-Host "[install] $Message" }
function Fail { param($Message) Write-Host "[install] ERROR: $Message" -ForegroundColor Red; exit 1 }

if (-not $Token) {
    Fail @"
missing enrollment token.

Set it and re-run:
  `$env:ORCH_TOKEN='<TOKEN>'; irm $Orchestrator/install.ps1 | iex

Mint one from the dashboard's "Add a node" dialog on the Overview page.
"@
}

$Orchestrator = $Orchestrator.TrimEnd('/')
Write-Step "orchestrator = $Orchestrator"

# --- Prerequisite: Python 3.11+ ----------------------------------------------

$pythonExe = $null
foreach ($candidate in @("python", "python3", "py")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
        $ok = & $candidate -c "import sys; print(1 if sys.version_info >= (3,11) else 0)" 2>$null
        if ($ok -eq "1") { $pythonExe = $candidate; break }
    } catch { continue }
}

if (-not $pythonExe) {
    Fail @"
Python 3.11 or newer was not found.

Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"
during setup), then close and reopen PowerShell and run this again.
"@
}
$pyVersion = & $pythonExe -c "import platform; print(platform.python_version())"
Write-Step "Python OK: $pythonExe ($pyVersion)"

# --- Reachability: fail here rather than after a long install ----------------

try {
    $health = Invoke-RestMethod -Uri "$Orchestrator/health" -TimeoutSec 15
    Write-Step "orchestrator reachable (db: $($health.db))"
} catch {
    Fail @"
could not reach $Orchestrator/health

Check the address is right and that this machine can reach it. If the
orchestrator is on someone else's network, they need to expose it (a tunnel or
an overlay network) — a plain LAN address will not work from outside their
network.
"@
}

# --- Optional: Docker. Present = full isolation; absent = unsandboxed path ---

$useDocker = $false
if (Get-Command docker -ErrorAction SilentlyContinue) {
    try {
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { $useDocker = $true }
    } catch { $useDocker = $false }
}

if ($useDocker) {
    Write-Step "Docker is available: jobs will run in isolated containers (ADR-007)"
} else {
    Write-Host ""
    Write-Host "  ----------------------------------------------------------------" -ForegroundColor Yellow
    Write-Host "  Docker was not found, so jobs will run WITHOUT container isolation" -ForegroundColor Yellow
    Write-Host "  ----------------------------------------------------------------" -ForegroundColor Yellow
    Write-Host "  Training jobs will run as ordinary processes under your user"
    Write-Host "  account. That means a job can read and write any file you can, has"
    Write-Host "  your network access, and is not limited by the kernel."
    Write-Host ""
    Write-Host "  The only program run this way is this project's own trainer"
    Write-Host "  (trainer/train.py), which you can read before agreeing. People"
    Write-Host "  submitting jobs cannot supply code - only a dataset name from a"
    Write-Host "  fixed list and range-checked numbers."
    Write-Host ""
    Write-Host "  Install Docker Desktop first if you would rather have full"
    Write-Host "  isolation: https://docs.docker.com/get-docker/"
    Write-Host ""
    $answer = Read-Host "  Continue without isolation? (yes/no)"
    if ($answer -ne "yes") {
        Write-Step "aborted at your request; nothing was installed."
        exit 0
    }
}

# --- GPU detection: honest either way ----------------------------------------

$hasGpu = $false
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    try {
        $gpuName = (nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
        if ($gpuName) { $hasGpu = $true; Write-Step "NVIDIA GPU detected: $gpuName" }
    } catch { }
}
if (-not $hasGpu) {
    Write-Step "no NVIDIA GPU detected - this node will enroll honestly as CPU-only."
}

# --- Download the agent bundle from the orchestrator that is bootstrapping us -

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$bundle = Join-Path $WorkDir "agent-bundle.tar.gz"

Write-Step "downloading agent bundle from $Orchestrator/agent-bundle.tar.gz"
try {
    Invoke-WebRequest -Uri "$Orchestrator/agent-bundle.tar.gz" -OutFile $bundle -TimeoutSec 120
} catch {
    Fail "failed to download the agent bundle from $Orchestrator"
}

Write-Step "extracting bundle into $WorkDir"
# tar.exe ships with Windows 10 1803+ and Windows 11.
tar -xzf $bundle -C $WorkDir
if ($LASTEXITCODE -ne 0) { Fail "failed to extract the agent bundle (is tar.exe available?)" }

# --- Install into an isolated virtualenv -------------------------------------

Write-Step "creating a virtualenv at $WorkDir\.venv"
& $pythonExe -m venv "$WorkDir\.venv"
if ($LASTEXITCODE -ne 0) { Fail "failed to create the agent virtualenv" }

$venvPy = Join-Path $WorkDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "virtualenv creation did not produce $venvPy" }

Write-Step "installing agent dependencies (a minute or so)"
& $venvPy -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { Fail "failed to upgrade pip in the agent virtualenv" }
& $venvPy -m pip install --quiet "$WorkDir[agent]"
if ($LASTEXITCODE -ne 0) { Fail "failed to install the agent's dependencies. Check network access to PyPI." }

if (-not $useDocker) {
    # The unsandboxed path runs train.py in this venv, so PyTorch must live here
    # rather than inside the trainer image.
    if ($hasGpu) {
        Write-Step "installing PyTorch with CUDA support (~2.5 GB, one time)"
        & $venvPy -m pip install --quiet torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
    } else {
        Write-Step "installing CPU-only PyTorch (~200 MB, one time)"
        & $venvPy -m pip install --quiet torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cpu
    }
    if ($LASTEXITCODE -ne 0) { Fail "failed to install PyTorch. Check network access and disk space." }
    & $venvPy -m pip install --quiet boto3==1.35.99
    if ($LASTEXITCODE -ne 0) { Fail "failed to install boto3 (needed for checkpointing)." }

    # Prove the install actually works before claiming success.
    $torchOk = & $venvPy -c "import torch; print(torch.cuda.is_available())" 2>$null
    Write-Step "PyTorch installed (CUDA available: $torchOk)"
    if ($hasGpu -and $torchOk -ne "True") {
        Write-Host "[install] WARNING: an NVIDIA GPU was detected but PyTorch cannot use it." -ForegroundColor Yellow
        Write-Host "[install]          This node will still contribute, on CPU. Updating your" -ForegroundColor Yellow
        Write-Host "[install]          NVIDIA driver usually fixes this." -ForegroundColor Yellow
    }
}

Write-Step "agent installed."

# --- Enroll and run ----------------------------------------------------------

Write-Step "starting the agent (enrolling, then heartbeating)"
Write-Step "state directory: $StateDir"
Write-Step "press Ctrl+C to stop sharing this machine."
Write-Host ""

$agentArgs = @("-m", "agent", "--orchestrator", $Orchestrator, "--enrollment-token", $Token, "--state-dir", $StateDir)
if (-not $useDocker) { $agentArgs += "--allow-unsandboxed" }

& $venvPy @agentArgs
exit $LASTEXITCODE
