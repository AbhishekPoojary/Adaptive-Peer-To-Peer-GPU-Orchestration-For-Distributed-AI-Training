# gpu-orchestrator agent installer for Windows (native PowerShell).
#
# Usage:
#   $env:ORCH_TOKEN='<ENROLLMENT_TOKEN>'
#   irm https://<orchestrator>/install.ps1 | iex
#
# Optional: $env:ORCH_URL overrides where the agent dials. It defaults to the
# address you fetched this script from, which the orchestrator substitutes in
# when it serves the file — so the common case needs no URL at all.
#
# Why this exists: install.sh needs WSL2 on Windows, and GPU passthrough inside
# WSL additionally needs the NVIDIA Container Toolkit. That is enough setup to
# lose most volunteers. This uses the PowerShell a Windows user already has,
# and — when Docker is absent — the agent's unsandboxed execution path
# (ADR-007 addendum), so the only prerequisite is Python.
#
# IMPORTANT STRUCTURE NOTE. Everything lives inside a function that `return`s.
# `irm | iex` executes in the *caller's* session, where a bare `exit` terminates
# the PowerShell host — closing the window and taking the error message with it.
# The first version did exactly that: a peer whose reachability check failed saw
# their terminal vanish with no explanation. Never add a top-level `exit` here,
# and never set $ErrorActionPreference outside the function (it would leak into
# the user's session and change how their shell behaves afterwards).

function Invoke-GpuOrchestratorInstall {
    $Token        = $env:ORCH_TOKEN
    # Replaced by the orchestrator at serve time with the address this script
    # was actually downloaded from. The literal placeholder only survives when
    # the file is run straight from a checkout.
    $servedFrom   = '__ORCHESTRATOR_URL__'
    $Orchestrator = if ($env:ORCH_URL) { $env:ORCH_URL }
                    elseif ($servedFrom -notmatch '^__') { $servedFrom }
                    else { 'http://localhost:8090' }
    $StateDir     = "$env:USERPROFILE\.gpu-orchestrator-agent"
    $WorkDir      = "$env:USERPROFILE\.gpu-orchestrator-agent-src"

    function Write-Step { param($m) Write-Host "[install] $m" }
    function Write-Fail {
        param($m)
        Write-Host ""
        Write-Host "[install] ERROR: $m" -ForegroundColor Red
        Write-Host ""
        Write-Host "[install] Nothing was left running. Fix the above and paste the command again." -ForegroundColor Yellow
    }

    if (-not $Token) {
        Write-Fail @"
No enrollment token.

Set it and re-run:
  `$env:ORCH_TOKEN='<TOKEN>'; irm $Orchestrator/install.ps1 | iex

Ask whoever runs the orchestrator for a token - they mint one from the
dashboard's "Add a node" button.
"@
        return
    }

    $Orchestrator = $Orchestrator.TrimEnd('/')
    Write-Step "orchestrator = $Orchestrator"

    # --- Python 3.11+ --------------------------------------------------------

    $pythonExe = $null
    foreach ($candidate in @("python", "python3", "py")) {
        if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        try {
            $ok = & $candidate -c "import sys; print(1 if sys.version_info >= (3,11) else 0)" 2>$null
            if ($ok -eq "1") { $pythonExe = $candidate; break }
        } catch { continue }
    }
    if (-not $pythonExe) {
        Write-Fail @"
Python 3.11 or newer was not found.

Install it from https://www.python.org/downloads/
IMPORTANT: tick "Add python.exe to PATH" during setup.
Then CLOSE this window, open a new PowerShell, and paste the command again.
"@
        return
    }
    $pyVersion = & $pythonExe -c "import platform; print(platform.python_version())"
    Write-Step "Python OK: $pythonExe ($pyVersion)"

    # --- Reachability: fail here, not after a long install -------------------

    try {
        $health = Invoke-RestMethod -Uri "$Orchestrator/health" -TimeoutSec 20
        Write-Step "orchestrator reachable (db: $($health.db))"
    } catch {
        Write-Fail @"
Could not reach $Orchestrator/health

  $($_.Exception.Message)

If that address says 'localhost', this script was not told where the
orchestrator lives. Ask for the full URL and set it:

  `$env:ORCH_URL='https://<their-address>'
  `$env:ORCH_TOKEN='<TOKEN>'
  irm `$env:ORCH_URL/install.ps1 | iex
"@
        return
    }

    # --- Docker is optional: present = isolation, absent = ask for consent ---

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
        Write-Host "  Docker was not found, so jobs would run WITHOUT container isolation" -ForegroundColor Yellow
        Write-Host "  ----------------------------------------------------------------" -ForegroundColor Yellow
        Write-Host "  Training jobs would run as ordinary processes under your user"
        Write-Host "  account. That means a job can read and write any file you can, has"
        Write-Host "  your network access, and is not limited by the kernel."
        Write-Host ""
        Write-Host "  The only program run this way is this project's own trainer"
        Write-Host "  (trainer/train.py), which you can read before agreeing. People"
        Write-Host "  submitting jobs cannot supply code - only a dataset name from a"
        Write-Host "  fixed list and range-checked numbers."
        Write-Host ""
        Write-Host "  Prefer full isolation? Install Docker Desktop first:"
        Write-Host "  https://docs.docker.com/get-docker/"
        Write-Host ""
        $answer = Read-Host "  Continue without isolation? (type yes to accept)"
        if ($answer -ne "yes") {
            Write-Step "Stopped at your request. Nothing was installed."
            return
        }
    }

    # --- GPU: honest either way ---------------------------------------------

    $hasGpu = $false
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        try {
            $gpuName = (nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
            if ($gpuName) { $hasGpu = $true; Write-Step "NVIDIA GPU detected: $gpuName" }
        } catch { }
    }
    if (-not $hasGpu) {
        Write-Step "No NVIDIA GPU detected - this machine will join honestly as a CPU node."
    }

    # --- Download the agent from the orchestrator bootstrapping us -----------

    try {
        New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
        $bundle = Join-Path $WorkDir "agent-bundle.tar.gz"
        Write-Step "downloading agent from $Orchestrator/agent-bundle.tar.gz"
        Invoke-WebRequest -Uri "$Orchestrator/agent-bundle.tar.gz" -OutFile $bundle -TimeoutSec 180

        Write-Step "extracting into $WorkDir"
        # tar.exe ships with Windows 10 1803+ and Windows 11.
        tar -xzf $bundle -C $WorkDir
        if ($LASTEXITCODE -ne 0) { throw "tar failed to extract the bundle (is tar.exe present?)" }
    } catch {
        Write-Fail "could not download or extract the agent: $($_.Exception.Message)"
        return
    }

    # --- Install into an isolated virtualenv ---------------------------------

    try {
        Write-Step "creating a virtualenv at $WorkDir\.venv"
        & $pythonExe -m venv "$WorkDir\.venv"
        $venvPy = Join-Path $WorkDir ".venv\Scripts\python.exe"
        if (-not (Test-Path $venvPy)) { throw "virtualenv creation did not produce $venvPy" }

        Write-Step "installing agent dependencies (about a minute)"
        & $venvPy -m pip install --quiet --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw "could not upgrade pip" }
        & $venvPy -m pip install --quiet "$WorkDir[agent]"
        if ($LASTEXITCODE -ne 0) { throw "could not install agent dependencies (check network access to PyPI)" }
    } catch {
        Write-Fail $_.Exception.Message
        return
    }

    if (-not $useDocker) {
        try {
            if ($hasGpu) {
                Write-Step "installing PyTorch with CUDA (~2.5 GB, one time - go get a coffee)"
                & $venvPy -m pip install --quiet torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
            } else {
                Write-Step "installing CPU-only PyTorch (~200 MB, one time)"
                & $venvPy -m pip install --quiet torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cpu
            }
            if ($LASTEXITCODE -ne 0) { throw "could not install PyTorch (check network access and free disk space)" }
            & $venvPy -m pip install --quiet boto3==1.35.99
            if ($LASTEXITCODE -ne 0) { throw "could not install boto3 (needed for checkpointing)" }

            # Prove it works rather than assuming it did.
            $torchOk = & $venvPy -c "import torch; print(torch.cuda.is_available())" 2>$null
            Write-Step "PyTorch installed (CUDA available: $torchOk)"
            if ($hasGpu -and $torchOk -ne "True") {
                Write-Host "[install] NOTE: a GPU was detected but PyTorch cannot use it." -ForegroundColor Yellow
                Write-Host "[install]       This machine will still contribute, on CPU." -ForegroundColor Yellow
                Write-Host "[install]       Updating your NVIDIA driver usually fixes it." -ForegroundColor Yellow
            }
        } catch {
            Write-Fail $_.Exception.Message
            return
        }
    }

    # --- Enroll and run ------------------------------------------------------

    Write-Step "starting the agent (enrolling, then heartbeating)"
    Write-Step "state directory: $StateDir"
    Write-Host ""
    Write-Host "  This window must stay open while you are sharing." -ForegroundColor Green
    Write-Host "  Press Ctrl+C to stop at any time." -ForegroundColor Green
    Write-Host ""

    $agentArgs = @(
        "-m", "agent",
        "--orchestrator", $Orchestrator,
        "--enrollment-token", $Token,
        "--state-dir", $StateDir
    )
    if (-not $useDocker) { $agentArgs += "--allow-unsandboxed" }

    & $venvPy @agentArgs

    if ($LASTEXITCODE -ne 0) {
        Write-Fail "the agent stopped with exit code $LASTEXITCODE (scroll up for the reason)."
    }
}

Invoke-GpuOrchestratorInstall
