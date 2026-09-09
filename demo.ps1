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
$minioPort = (Select-String -Path $envFile -Pattern '^MINIO_API_PORT=(\d+)').Matches.Groups[1].Value
if (-not $minioPort) { $minioPort = "9000" }
$adminKey = (Select-String -Path $envFile -Pattern '^ADMIN_API_KEY=(.+)$').Matches.Groups[1].Value
$orchPort = (Select-String -Path $envFile -Pattern '^ORCHESTRATOR_PORT=(\d+)').Matches.Groups[1].Value
if (-not $orchPort) { $orchPort = "8090" }

$wantEndpoint = "S3_ENDPOINT_URL=http://${lanIp}:${minioPort}"
$envLines = Get-Content $envFile
if ($envLines -match '^S3_ENDPOINT_URL=') {
    $envLines = $envLines -replace '^S3_ENDPOINT_URL=.*', $wantEndpoint
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

# --- 6. Share this machine's GPU --------------------------------------------
# The agent runs here rather than being installed by hand. The S3_* variables
# are what make the trained model retrievable afterwards: without them the
# trainer runs perfectly and saves nothing, and the job page has no model to
# hand back -- a silent failure that looks like success.
$token = (Invoke-RestMethod -Uri "http://localhost:${orchPort}/auth/enrollment-tokens" `
    -Method Post -Headers @{ "X-Admin-Key" = $adminKey } `
    -ContentType "application/json" `
    -Body (@{ created_by = "demo.ps1"; ttl_seconds = 3600 } | ConvertTo-Json)).token

$s3User = (Select-String -Path $envFile -Pattern '^MINIO_ROOT_USER=(.+)$').Matches.Groups[1].Value
$s3Pass = (Select-String -Path $envFile -Pattern '^MINIO_ROOT_PASSWORD=(.+)$').Matches.Groups[1].Value
$agentPy = Join-Path $env:USERPROFILE ".gpu-orchestrator-agent-src\.venv\Scripts\python.exe"

# A stale identity here is why enrolment fails after the database is reset: the
# agent finds saved state, tries to resume as a node the orchestrator no longer
# knows, and never uses the fresh token at all. Starting clean each run costs
# one extra row in the node list and removes that whole failure mode.
$stateDir = Join-Path $env:USERPROFILE ".gpu-orchestrator-agent"
if (Test-Path $stateDir) { Remove-Item -Recurse -Force $stateDir }

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
        "--allow-unsandboxed",
        "--s3-endpoint-url", "http://${lanIp}:${minioPort}",
        "--s3-access-key", $s3User,
        "--s3-secret-key", $s3Pass,
        "--s3-bucket-checkpoints", "checkpoints"
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
        "`$env:ORCH_URL='http://${lanIp}:${orchPort}'",
        "`$env:S3_ENDPOINT_URL='http://${lanIp}:${minioPort}'",
        "`$env:S3_ACCESS_KEY='$s3User'",
        "`$env:S3_SECRET_KEY='$s3Pass'",
        "`$env:S3_BUCKET_CHECKPOINTS='checkpoints'"
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
    Say "  Opening a public tunnel..." "Gray"
    $cfLog = Join-Path $env:TEMP "gpu-orch-tunnel.log"
    Remove-Item $cfLog -ErrorAction SilentlyContinue
    Start-Process $cf -ArgumentList "tunnel", "--url", "http://localhost:5173" `
        -RedirectStandardError $cfLog -RedirectStandardOutput "$cfLog.out" -WindowStyle Hidden

    $deadline = (Get-Date).AddMinutes(2)
    do {
        Start-Sleep -Seconds 2
        $found = Select-String -Path $cfLog, "$cfLog.out" -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' `
            -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { $publicUrl = $found.Matches[0].Value }
    } while (-not $publicUrl -and (Get-Date) -lt $deadline)

    if (-not $publicUrl) {
        Say "  ! The tunnel did not come up. Continuing with the LAN link only." "Yellow"
    } else {
        $tunnelHost = ([Uri]$publicUrl).Host
        Say "  Public link    $publicUrl" "Green"
    }
}

# --- 8. The dashboard --------------------------------------------------------
# Vite refuses a Host header it does not recognise, so every hostname it will be
# reached by has to be allow-listed explicitly -- otherwise a remote browser
# gets a bare "Blocked request" that looks like the app being down.
Say "  Starting the dashboard..." "Gray"
$dash = Join-Path $repo "dashboard"

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
Start-Process powershell -WorkingDirectory $dash -ArgumentList "-NoExit", "-Command", `
    "`$env:VITE_ALLOWED_HOSTS='$allowedList'; npm run dev -- --host 0.0.0.0 --strictPort"

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
} elseif (-not $Public) {
    Say ""
    Say "  That link only works on this Wi-Fi. For someone on a different" "Gray"
    Say "  network, re-run with:  powershell -ExecutionPolicy Bypass -File demo.ps1 -Public" "Gray"
}
Say ""
