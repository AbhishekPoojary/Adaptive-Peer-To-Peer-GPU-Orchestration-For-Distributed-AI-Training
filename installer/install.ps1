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
    # Substituted by the orchestrator at serve time with the image THIS
    # deployment uses, so a peer is never told it out of band and the fleet
    # cannot drift onto mixed images. The literal placeholder only survives
    # when the file is run straight from a checkout.
    $servedImage  = '__TRAINER_IMAGE__'
    $TrainerImage = if ($servedImage -notmatch '^__') { $servedImage }
                    else { 'gpu-orchestrator-trainer:latest' }
    # The bundle is extracted later, but the image check below may need to
    # build from it; same path, named separately so the intent is obvious.
    $WorkDirPending = $WorkDir

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

    # --- Python 3.11-3.13 ----------------------------------------------------
    #
    # The upper bound is real (see pyproject.toml): pydantic-core ships Windows
    # wheels only up to cp313, so on 3.14 pip tries to compile it from Rust and
    # dies without a MSVC linker. A volunteer hit exactly that.
    #
    # Windows machines commonly have several Pythons, so this looks for a
    # COMPATIBLE one rather than taking the first it finds — `py -3.13` often
    # exists alongside a too-new `python`.

    $pythonExe = $null
    $pythonPre = @()
    $seen = @()
    $candidates = @(
        @("py", @("-3.13")), @("py", @("-3.12")), @("py", @("-3.11")),
        @("python", @()), @("python3", @()), @("py", @())
    )
    foreach ($entry in $candidates) {
        $exe = $entry[0]; $pre = $entry[1]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $probe = & $exe @pre -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if (-not $probe) { continue }
            $seen += $probe
            $parts = $probe.Split('.')
            $maj = [int]$parts[0]; $min = [int]$parts[1]
            if ($maj -eq 3 -and $min -ge 11 -and $min -le 13) {
                $pythonExe = $exe
                $pythonPre = $pre
                break
            }
        } catch { continue }
    }

    if (-not $pythonExe) {
        $found = if ($seen.Count) { ($seen | Sort-Object -Unique) -join ', ' } else { 'none' }
        Write-Fail @"
No compatible Python found. Need 3.11, 3.12 or 3.13 (found: $found).

Python 3.14 does NOT work yet: one of our dependencies has no prebuilt package
for it, so your machine would have to compile it from source - which needs
Visual Studio build tools. Not worth it; just install 3.13 alongside.

  1. Get 3.13 from https://www.python.org/downloads/release/python-3130/
  2. Tick "Add python.exe to PATH" during setup
  3. CLOSE this window, open a new PowerShell, paste the command again

Installing 3.13 will not remove or break the Python you already have.
"@
        return
    }
    $pyVersion = & $pythonExe @pythonPre -c "import platform; print(platform.python_version())"
    Write-Step "Python OK: $pythonExe $($pythonPre -join ' ') ($pyVersion)"

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

    # --- Docker is optional: present = isolation, absent = ask for consent ---

    $useDocker = $false
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        try {
            docker info *> $null
            if ($LASTEXITCODE -eq 0) { $useDocker = $true }
        } catch { $useDocker = $false }
    }

    if ($useDocker) {
        # Docker being installed is not the same fact as Docker being able to
        # run our trainer. The image is built locally and published to no
        # registry, so a peer that has never built it gets a bare registry 404
        # ("pull access denied") on its first claimed lease — which reads like
        # an auth problem and is nothing of the sort. A real peer hit exactly
        # that, so this is checked before anything is installed.
        docker image inspect $TrainerImage *> $null
        if ($LASTEXITCODE -ne 0) {
            # A published image just works: pull it and carry on with full
            # isolation. Only fall through to the awkward choice below when the
            # image genuinely cannot be obtained (i.e. it was never published).
            Write-Step "trainer image not local; trying to pull $TrainerImage"
            docker pull $TrainerImage *> $null
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host "  ----------------------------------------------------------------" -ForegroundColor Yellow
            Write-Host "  Docker works here, but the trainer image is not on this machine" -ForegroundColor Yellow
            Write-Host "  ----------------------------------------------------------------" -ForegroundColor Yellow
            Write-Host "  The image ($TrainerImage) is built locally by whoever"
            Write-Host "  runs the orchestrator and is not published anywhere, so Docker"
            Write-Host "  cannot download it. Two ways forward:"
            Write-Host ""
            Write-Host "   [1] Run without a container  (recommended)" -ForegroundColor Green
            Write-Host "       Downloads only PyTorch (~200 MB CPU / ~2.5 GB CUDA)."
            Write-Host "       Jobs run as normal processes under your account - no"
            Write-Host "       container isolation. Details shown before you confirm."
            Write-Host ""
            Write-Host "   [2] Build the image here"
            Write-Host "       Full container isolation, but pulls a ~7 GB CUDA base"
            Write-Host "       image first and needs GPU passthrough set up for Docker."
            Write-Host ""
            $choice = Read-Host "  Choose 1 or 2"
            if ($choice -eq "2") {
                Write-Step "building $TrainerImage (this pulls several GB the first time)"
                docker build -t $TrainerImage -f "$WorkDirPending	rainer\Dockerfile" $WorkDirPending
                if ($LASTEXITCODE -ne 0) {
                    Write-Fail "the image build failed (scroll up). You can re-run and choose 1 instead."
                    return
                }
                Write-Step "image built: jobs will run in isolated containers"
            } else {
                Write-Step "will run without container isolation"
                $useDocker = $false
            }
        } else {
            Write-Step "Docker is available and the trainer image is present (ADR-007 isolation)"
        }
    }
    if (-not $useDocker) {
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

    # --- Install into an isolated virtualenv ---------------------------------

    try {
        Write-Step "creating a virtualenv at $WorkDir\.venv"
        & $pythonExe @pythonPre -m venv "$WorkDir\.venv"
        $venvPy = Join-Path $WorkDir ".venv\Scripts\python.exe"
        if (-not (Test-Path $venvPy)) { throw "virtualenv creation did not produce $venvPy" }

        Write-Step "installing agent dependencies (about a minute)"
        & $venvPy -m pip install --quiet --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw "could not upgrade pip" }
        & $venvPy -m pip install "$WorkDir[agent]"
        if ($LASTEXITCODE -ne 0) {
            throw @"
could not install the agent's dependencies.

If the output above mentions 'link.exe not found', 'cargo', 'maturin' or
'Building wheel ... did not run successfully', your Python version has no
prebuilt package for one of our dependencies and pip tried to compile it.
Install Python 3.13 and run this again - see the note above.

Otherwise, check this machine can reach https://pypi.org.
"@
        }
    } catch {
        Write-Fail $_.Exception.Message
        return
    }

    if (-not $useDocker) {
        try {
            # Deliberately a RANGE, not the trainer image's exact 2.5.1 pin. That
            # pin is right for the container (the image is fixed and always
            # available) and wrong here: PyTorch publishes no Windows wheel for
            # torch 2.5.1 on Python 3.13, so a peer on 3.13 could not install it
            # at all. Verified against the index, not guessed — 2.6.0 is the
            # first release with a cp313 win_amd64 build. The trainer uses
            # standard APIs and runs unchanged on 2.6+ (this project's own dev
            # machine is on 2.6.0).
            if ($hasGpu) {
                Write-Step "installing PyTorch with CUDA (~2.5 GB, one time - go get a coffee)"
                & $venvPy -m pip install "torch>=2.6,<3" "torchvision>=0.21,<1" --index-url https://download.pytorch.org/whl/cu124
            } else {
                Write-Step "installing CPU-only PyTorch (~200 MB, one time)"
                & $venvPy -m pip install "torch>=2.6,<3" "torchvision>=0.21,<1" --index-url https://download.pytorch.org/whl/cpu
            }
            if ($LASTEXITCODE -ne 0) {
                throw @"
could not install PyTorch.

If the output above says 'Could not find a version that satisfies', PyTorch
publishes no build for this Python version on this platform. Python 3.13 is
known good; 3.14 is not supported by PyTorch yet.

Otherwise check network access to download.pytorch.org and free disk space
(the CUDA build needs ~3 GB).
"@
            }
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
        "--state-dir", $StateDir,
        "--trainer-image", $TrainerImage
    )
    if (-not $useDocker) { $agentArgs += "--allow-unsandboxed" }

    & $venvPy @agentArgs

    if ($LASTEXITCODE -ne 0) {
        Write-Fail "the agent stopped with exit code $LASTEXITCODE (scroll up for the reason)."
    }
}

Invoke-GpuOrchestratorInstall
