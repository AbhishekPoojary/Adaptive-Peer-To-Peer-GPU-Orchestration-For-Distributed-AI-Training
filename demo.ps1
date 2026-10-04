# Start everything needed to share this machine's GPU with someone else.
#
# One command on this side. Nothing at all on theirs: they open a link in a
# browser, sign in, upload a dataset, and download the trained model.
#
# The settings that used to need hand-editing are all derived here instead --
# most importantly this machine's LAN address, which changes whenever the
# network does and silently breaks dataset downloads when it is stale.
#
#   powershell -ExecutionPolicy Bypass -File demo.ps1
#
# Stop everything with Ctrl+C, then:  docker compose -f deploy/compose.yaml down

param(
    # Publish a public https:// link via a Cloudflare quick tunnel, so the other
    # person can be on any network anywhere. Without this the link is a LAN
    # address that only works if you are both on the same Wi-Fi.
    [switch]$Public
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
Set-Location $repo

function Say([string]$text, [string]$colour = "White") {
    Write-Host $text -ForegroundColor $colour
}

# Windows PowerShell 5.1 turns a native command's stderr into ErrorRecords, and
# `docker compose` reports ordinary progress ("Container ... Running") there. With
# ErrorActionPreference = Stop that makes a perfectly successful command abort the
# script. Neither `2>&1 | Out-Null` nor `*> $null` avoids it -- the preference
# itself has to be relaxed for the duration of the call. Exit code is what
# actually decides success, so it is returned for the caller to check.
function Invoke-Native([scriptblock]$Command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command 2>&1 | Out-Null; return $LASTEXITCODE }
    finally { $ErrorActionPreference = $previous }
}

Say ""
Say "  GPU Orchestrator - starting a shareable fleet" "Cyan"
Say "  ---------------------------------------------" "Cyan"

# --- 1. This machine's LAN address -------------------------------------------
# Everything a peer or a browser is handed must name an address they can reach,
# so a loopback or a virtual-adapter address is useless here. Docker Desktop and
# WSL both add adapters that look plausible and route nowhere, hence the filter.
$candidates = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object {
        $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and
        $_.InterfaceAlias -notmatch 'Loopback|WSL|vEthernet|Hyper-V'
    } |
    Sort-Object -Property SkipAsSource, InterfaceMetric

if (-not $candidates) {
    Say "  ! No usable network address found. Are you connected to a network?" "Red"
    exit 1
}
$lanIp = $candidates[0].IPAddress
$adapter = $candidates[0].InterfaceAlias
Say ""
Say "  Address        $lanIp  (via $adapter)" "Green"

# --- 2. Docker ---------------------------------------------------------------
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Say "  Docker Desktop is not running - starting it..." "Yellow"
    $dockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $dockerExe)) {
        Say "  ! Docker Desktop not found. Install it, then run this again." "Red"
        exit 1
    }
    Start-Process $dockerExe
    Say "  Waiting for the Docker engine (this can take a minute)..." "Yellow"
    do { Start-Sleep -Seconds 3; docker info *> $null } while ($LASTEXITCODE -ne 0)
}
Say "  Docker         ready" "Green"

# --- 3. Point object storage at an address peers can resolve -----------------
# The orchestrator signs dataset URLs against this endpoint, and a signature
# covers the host it was signed for -- so a stale value here is the single most
# common cause of a peer failing with "getaddrinfo failed" before it downloads
# a byte. Rewritten on every run so it cannot drift from the current network.
$envFile = Join-Path $repo "deploy\.env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $repo "deploy\.env.example") $envFile
    Say "  Created deploy\.env from the example" "Yellow"
}
# The example file ships placeholder secrets that are public (they are in this
# repository). Left in place, anyone could forge an admin sign-in token
# (JWT_SIGNING_KEY) or enrol machines (ADMIN_API_KEY) -- and -Public puts this
# on the internet. Replace any still-default value with a random one. The only
# cost is that existing sign-ins end once, the first time this runs.
function New-Secret {
    # Cryptographic randomness: these are signing keys, not shuffles.
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $_.ToString("x2") })
}
$defaults = @{ "JWT_SIGNING_KEY" = "dev-only-change-me"; "ADMIN_API_KEY" = "dev-admin-key-change-me" }
$envText = Get-Content $envFile
$changed = $false
foreach ($name in $defaults.Keys) {
    if ($envText -match "^$name=$([regex]::Escape($defaults[$name]))$") {
        $envText = $envText -replace "^$name=.*", "$name=$(New-Secret)"
        $changed = $true
    }
}
if ($changed) {
    Set-Content -Path $envFile -Value $envText -Encoding utf8
    Say "  Secrets        replaced the example placeholders with random values" "Green"
}

$minioPort = (Select-String -Path $envFile -Pattern '^MINIO_API_PORT=(\d+)').Matches.Groups[1].Value
if (-not $minioPort) { $minioPort = "9000" }
$adminKey = (Select-String -Path $envFile -Pattern '^ADMIN_API_KEY=(.+)$').Matches.Groups[1].Value
$orchPort = (Select-String -Path $envFile -Pattern '^ORCHESTRATOR_PORT=(\d+)').Matches.Groups[1].Value
if (-not $orchPort) { $orchPort = "8090" }

# This machine's LAN address is what peers on the same network reach storage
# by, so it signs their dataset links -- S3_PUBLIC_ENDPOINT_URL. It must NOT go
# in S3_ENDPOINT_URL, which is the orchestrator's own route to MinIO: pinned
# to a LAN address, every checkpoint failed with 503 the day the address
# changed (ADR-006 addendum 3). Any such line from older runs is removed, so
# compose's internal default (http://minio:9000) applies.
$wantEndpoint = "S3_PUBLIC_ENDPOINT_URL=http://${lanIp}:${minioPort}"
$envLines = @(Get-Content $envFile | Where-Object { $_ -notmatch '^S3_ENDPOINT_URL=' })
if ($envLines -match '^S3_PUBLIC_ENDPOINT_URL=') {
    $envLines = $envLines -replace '^S3_PUBLIC_ENDPOINT_URL=.*', $wantEndpoint
} else {
    $envLines += $wantEndpoint
}
Set-Content -Path $envFile -Value $envLines -Encoding utf8
Say "  Storage        http://${lanIp}:${minioPort}" "Green"

# --- 4. Firewall -------------------------------------------------------------
# Both networks on a typical laptop are classified Public, where Windows drops
# unsolicited inbound by default -- which looks exactly like the app being down.
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

$ports = @{ "GPU Orchestrator API" = $orchPort; "GPU Orchestrator Storage" = $minioPort; "GPU Orchestrator Dashboard" = "5173" }
if ($isAdmin) {
    foreach ($name in $ports.Keys) {
        if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $name -Direction Inbound `
                -LocalPort $ports[$name] -Protocol TCP -Action Allow -Profile Any | Out-Null
        }
    }
    Say "  Firewall       ports $orchPort, $minioPort, 5173 allowed" "Green"
} else {
    Say "  Firewall       NOT configured - not running as administrator" "Yellow"
    Say "                 If your friend cannot connect, run this once in an" "Yellow"
    Say "                 admin PowerShell:" "Yellow"
    foreach ($name in $ports.Keys) {
        Say "                   New-NetFirewallRule -DisplayName '$name' -Direction Inbound -LocalPort $($ports[$name]) -Protocol TCP -Action Allow -Profile Any" "DarkGray"
    }
}

# --- 5. The stack ------------------------------------------------------------
Say "  Starting the orchestrator, database and storage..." "Gray"
$composeFile = Join-Path $repo "deploy\compose.yaml"
$code = Invoke-Native { docker compose -f $composeFile up -d }
if ($code -ne 0) {
    Say "  ! docker compose failed (exit $code). Try: docker compose -f deploy/compose.yaml up" "Red"
    exit 1
}
$health = "http://localhost:${orchPort}/health"
$deadline = (Get-Date).AddMinutes(3)
do {
    Start-Sleep -Seconds 2
    try { $ok = (Invoke-RestMethod -Uri $health -TimeoutSec 3).status -eq "ok" } catch { $ok = $false }
    if ((Get-Date) -gt $deadline) {
        Say "  ! The orchestrator did not become healthy. Try: docker compose -f deploy/compose.yaml logs orchestrator" "Red"
        exit 1
    }
} while (-not $ok)
Say "  Orchestrator   healthy on port $orchPort" "Green"

# --- 5a. The first admin account ---------------------------------------------
# A fresh install has no accounts. Anyone can create an ordinary account on the
# sign-in page, but adding machines and managing people needs an admin, and
# making one used to mean a Python environment, a database URL and a script.
# Asked for here instead, once, on the first run only.
$admins = docker exec deploy-postgres-1 psql -U orchestrator -d orchestrator -tAc `
    "select count(*) from users where role = 'ADMIN' and disabled_at is null" 2>$null
if ("$admins".Trim() -eq "0") {
    Say ""
    Say "  First run: create your admin account (you sign in with this)." "Cyan"
    do { $adminUser = Read-Host "    Choose a username (letters, numbers, . _ -)" } while (-not $adminUser)
    do {
        $first = Read-Host "    Choose a password (at least 12 characters)" -AsSecureString
        $second = Read-Host "    Type it again" -AsSecureString
        $p1 = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($first))
        $p2 = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($second))
        $okPassword = ($p1 -eq $p2) -and ($p1.Length -ge 12)
        if (-not $okPassword) { Say "    Those didn't match, or were shorter than 12 characters. Try again." "Yellow" }
    } while (-not $okPassword)
    # Passed by environment variable, never as an argument, so it does not
    # land in shell history (scripts/create_user.py reads ORCH_USER_PASSWORD).
    $code = Invoke-Native { docker exec -e "ORCH_USER_PASSWORD=$p1" deploy-orchestrator-1 python -m scripts.create_user --username $adminUser --role ADMIN --no-prompt }
    $p1 = $null; $p2 = $null
    if ($code -ne 0) {
        Say "  ! Could not create the account (exit $code). Run this again to retry." "Red"
        exit 1
    }
    Say "  Admin account  $adminUser created" "Green"
}

# --- 5b. Keep the trainer image in step with the source ----------------------
# When Docker is available the agent runs training in a container, so the image
# -- not the checkout -- is what actually executes. A stale image therefore
# fails in a way that looks nothing like "your image is old": a custom-dataset
# job dies with "unsupported DATASET 'custom'; expected cifar10 or mnist",
# because that image predates uploaded datasets entirely. Rebuild whenever the
# trainer source is newer than the image.
#
# Both tags are written on purpose. The agent's default is the bare
# `gpu-orchestrator-trainer:latest`, while deploy/.env names the namespaced one;
# tagging only one leaves the other pointing at the old build, which is exactly
# how this was missed the first time.
$trainerSrc = Join-Path $repo "trainer\train.py"
$imageBuilt = docker image inspect gpu-orchestrator-trainer:latest --format "{{.Created}}" 2>$null
$needsBuild = $true
if ($LASTEXITCODE -eq 0 -and $imageBuilt) {
    $needsBuild = ([datetime]$imageBuilt) -lt (Get-Item $trainerSrc).LastWriteTimeUtc
}
if ($needsBuild) {
    Say "  Trainer image  out of date - rebuilding (usually under a minute)" "Yellow"
    $code = Invoke-Native { docker build -f (Join-Path $repo "trainer\Dockerfile") -t gpu-orchestrator-trainer:latest $repo }
    if ($code -ne 0) {
        Say "  ! Trainer image build failed. Custom datasets will not run." "Red"
        exit 1
    }
    Invoke-Native { docker tag gpu-orchestrator-trainer:latest abhisheks1290/gpu-orchestrator-trainer:latest } | Out-Null
    Say "  Trainer image  rebuilt from current source" "Green"
} else {
    Say "  Trainer image  up to date" "Green"
}

# --- 6. Share this machine's GPU --------------------------------------------
# The agent runs here rather than being installed by hand. Checkpoints (and so
# the downloadable model) go through the orchestrator under a lease token
# (ADR-006 addendum 3), so the agent needs no storage credentials of its own.
$token = (Invoke-RestMethod -Uri "http://localhost:${orchPort}/auth/enrollment-tokens" `
    -Method Post -Headers @{ "X-Admin-Key" = $adminKey } `
    -ContentType "application/json" `
    -Body (@{ created_by = "demo.ps1"; ttl_seconds = 3600 } | ConvertTo-Json)).token

$agentPy = Join-Path $env:USERPROFILE ".gpu-orchestrator-agent-src\.venv\Scripts\python.exe"

# The saved identity is kept, so this laptop stays one machine across runs and
# keeps its reliability history. It used to be wiped every run -- after a
# database reset the agent would resume as a node the orchestrator no longer
# knew -- which added a duplicate machine to the list on every start. The agent
# now handles that itself: if its saved identity is refused, it enrolls afresh
# with the token passed below.

if (Test-Path $agentPy) {
    # The trainer image is built locally and published nowhere, so Docker cannot
    # pull it; jobs therefore run as ordinary child processes. Saying so out loud
    # rather than burying it, because the installer normally asks for consent
    # here and this path does not.
    Say "  Sharing GPU    starting the agent (jobs run as normal processes)" "Green"
    $agentArgs = @(
        "-m", "agent",
        "--orchestrator", "http://${lanIp}:${orchPort}",
        "--enrollment-token", $token,
        "--allow-unsandboxed"
    ) -join " "
    Start-Process powershell -WorkingDirectory (Join-Path $env:USERPROFILE ".gpu-orchestrator-agent-src") `
        -ArgumentList "-NoExit", "-Command", "& '$agentPy' $agentArgs"
} else {
    # No agent installed yet. Fall back to the installer, which downloads
    # PyTorch and asks two questions.
    Say "  Sharing GPU    no agent installed - running the installer" "Yellow"
    Say "                 It will ask two questions: answer 1, then yes" "Yellow"
    $envAssignments = @(
        "`$env:ORCH_TOKEN='$token'",
        "`$env:ORCH_URL='http://${lanIp}:${orchPort}'"
    ) -join "; "
    Start-Process powershell -ArgumentList "-NoExit", "-Command", `
        "$envAssignments; irm http://${lanIp}:${orchPort}/install.ps1 | iex"
}

# --- 7. A public link, if asked ----------------------------------------------
# The tunnel has to come up BEFORE the dashboard, because Vite needs the
# hostname allow-listed at start-up. Getting that order wrong produces a bare
# "Blocked request", which reads as a broken tunnel and is not one.
$tunnelHost = $null
if ($Public) {
    $cf = @(
        "C:\Program Files (x86)\cloudflared\cloudflared.exe",
        "C:\Program Files\cloudflared\cloudflared.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $cf) {
        Say "  ! cloudflared not found. Install it with:" "Red"
        Say "      winget install --id Cloudflare.cloudflared" "Yellow"
        exit 1
    }
    # One quick tunnel per local port; returns its https:// address or $null.
    function Open-Tunnel([int]$port, [string]$name) {
        $log = Join-Path $env:TEMP "gpu-orch-tunnel-$name.log"
        Remove-Item $log, "$log.out" -ErrorAction SilentlyContinue
        Start-Process $cf -ArgumentList "tunnel", "--no-autoupdate", "--url", "http://localhost:$port" `
            -RedirectStandardError $log -RedirectStandardOutput "$log.out" -WindowStyle Hidden
        $deadline = (Get-Date).AddMinutes(2)
        do {
            Start-Sleep -Seconds 2
            $found = Select-String -Path $log, "$log.out" -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' `
                -ErrorAction SilentlyContinue | Select-Object -First 1
        } while (-not $found -and (Get-Date) -lt $deadline)
        if ($found) { return $found.Matches[0].Value }
        return $null
    }

    Say "  Opening public tunnels..." "Gray"
    $publicUrl = Open-Tunnel 5173 "dashboard"
    # The orchestrator gets a tunnel of its own, for peers' agents. Cloudflare
    # terminates TLS with a certificate every machine already trusts, so a
    # peer's tokens, heartbeats, logs and checkpoints travel encrypted with
    # nothing to install or configure -- unlike the LAN address, which is
    # plain HTTP. The installer served through it names the https:// address
    # itself (X-Forwarded-Proto), so the agent dials that and nothing else.
    $apiUrl = Open-Tunnel $orchPort "orchestrator"

    if (-not $publicUrl) {
        Say "  ! The dashboard tunnel did not come up. Continuing with the LAN link only." "Yellow"
    } else {
        $tunnelHost = ([Uri]$publicUrl).Host
        Say "  Public link    $publicUrl" "Green"
    }
    if ($apiUrl) {
        Say "  Peers join at  $apiUrl  (HTTPS)" "Green"
    } else {
        Say "  ! The orchestrator tunnel did not come up; peers would join over plain HTTP." "Yellow"
    }
}

# --- 8. The dashboard --------------------------------------------------------
# Vite refuses a Host header it does not recognise, so every hostname it will be
# reached by has to be allow-listed explicitly -- otherwise a remote browser
# gets a bare "Blocked request" that looks like the app being down.
Say "  Starting the dashboard..." "Gray"
$dash = Join-Path $repo "dashboard"

# A fresh download has no dashboard dependencies yet; fetch them once.
if (-not (Test-Path (Join-Path $dash "node_modules"))) {
    Say "  First run: installing the dashboard (a minute or two, once)..." "Yellow"
    $code = Invoke-Native { npm --prefix $dash install --no-audit --no-fund }
    if ($code -ne 0) {
        Say "  ! npm install failed. Is Node.js installed? https://nodejs.org (LTS)" "Red"
        exit 1
    }
}

# A dashboard left over from a previous run keeps port 5173, and Vite quietly
# moves to 5174 rather than failing. The tunnel still points at 5173, so the
# link then serves the STALE server -- which does not have the new hostname
# allow-listed and answers "Blocked request". Clear the port first, and pass
# --strictPort so a port clash is a loud failure rather than a silent move.
Get-Process node -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

$allowed = @($lanIp, "localhost")
if ($tunnelHost) { $allowed = @($tunnelHost) + $allowed }
$allowedList = $allowed -join ","
# With a public orchestrator address, the dashboard's "Add a node" command
# hands peers that HTTPS address rather than this machine's LAN one.
$orchEnv = if ($apiUrl) { "`$env:VITE_ORCHESTRATOR_URL='$apiUrl'; " } else { "" }
Start-Process powershell -WorkingDirectory $dash -ArgumentList "-NoExit", "-Command", `
    "$orchEnv`$env:VITE_ALLOWED_HOSTS='$allowedList'; npm run dev -- --host 0.0.0.0 --strictPort"

$dashUrl = if ($publicUrl) { $publicUrl } else { "http://${lanIp}:5173" }
$deadline = (Get-Date).AddMinutes(2)
do {
    Start-Sleep -Seconds 2
    try { $up = (Invoke-WebRequest -Uri $dashUrl -TimeoutSec 8 -UseBasicParsing).StatusCode -eq 200 }
    catch { $up = $false }
} while (-not $up -and (Get-Date) -lt $deadline)

Say ""
Say "  =====================================================" "Cyan"
if ($up) {
    Say "   Send your friend this link:" "White"
    Say ""
    Say "       $dashUrl" "Green"
} else {
    Say "   The dashboard is still starting. It will be at:" "White"
    Say ""
    Say "       $dashUrl" "Yellow"
}
Say ""
Say "   They sign in, upload a .zip dataset, submit a job," "Gray"
Say "   and download the model when it finishes." "Gray"
Say "   They install nothing." "Gray"
Say ""
Say "   Their dataset zip must look like:" "Gray"
Say "       train/cat/*.jpg   train/dog/*.jpg" "DarkGray"
Say "       test/cat/*.jpg    test/dog/*.jpg" "DarkGray"
Say "   Check one first with:  python check_dataset.py theirs.zip" "DarkGray"
Say "  =====================================================" "Cyan"
Say ""
Say "  Two windows opened: the GPU agent and the dashboard." "Gray"
Say "  Keep both open. Closing the agent window stops sharing." "Gray"
if ($publicUrl) {
    Say ""
    Say "  The public link is live until you stop it. A quick tunnel gets a" "Gray"
    Say "  new address every restart, so it is a demo link, not a permanent" "Gray"
    Say "  one. Stop it with:  Get-Process cloudflared | Stop-Process" "Gray"
    Say ""
    # The tunnel cuts off any single request running longer than a minute or
    # two, so the dashboard sends archives in 4 MiB pieces and large uploads
    # go through. Speed is another matter: 346 MB at the ~150 KB/s measured
    # over a home upstream is roughly forty minutes, against 2.5s on the LAN.
    Say "  Datasets upload in pieces over the tunnel, so large archives work" "Gray"
    Say "  -- but at your upload speed, not your disk speed. Upload big ones" "Gray"
    Say "  yourself at $(if ($lanIp) { "http://${lanIp}:5173" } else { 'http://localhost:5173' }); those bytes never leave the machine." "Gray"
} elseif (-not $Public) {
    Say ""
    Say "  That link only works on this Wi-Fi. For someone on a different" "Gray"
    Say "  network, re-run with:  powershell -ExecutionPolicy Bypass -File demo.ps1 -Public" "Gray"
}
Say ""
