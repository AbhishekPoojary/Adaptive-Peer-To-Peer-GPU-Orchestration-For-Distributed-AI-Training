#!/usr/bin/env bash
# Set up (or update) the whole system on a fresh Ubuntu cloud server.
#
#   bash deploy/cloud/setup.sh
#
# Written for a small Ubuntu cloud server -- Azure for Students
# (docs/DEPLOY-AZURE.md) or Oracle Cloud Always Free (docs/DEPLOY-ORACLE.md) --
# and safe to run again at any time: it keeps your settings, data and
# certificates, and rebuilds whatever changed. To update to the latest version:
#
#   git pull && bash deploy/cloud/setup.sh
#
# It runs in the background: closing the window or losing the connection does
# not stop it. Its progress is in ~/orchestrator-setup.log.
#
# What it does, in order:
#   1. adds swap space on a small server, and installs Docker if it is missing;
#   2. opens the web ports in the server's own firewall (Oracle's Ubuntu
#      images block everything but SSH by default);
#   3. works out this server's permanent web addresses from its public IP;
#   4. on first run, writes deploy/cloud/.env with freshly generated secrets;
#   5. builds and starts everything;
#   6. on first run, creates your admin account (asked for at the start);
#   7. prints the links to share.

set -euo pipefail

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
ENV_FILE="deploy/cloud/.env"

[ "$(uname -s)" = "Linux" ] || die "run this on the Linux cloud server, not on your own computer."
command -v sudo >/dev/null || die "sudo is required."

LOG="$HOME/orchestrator-setup.log"
LOCK="$REPO/deploy/cloud/.setup.lock"
ADMIN_PENDING="$REPO/deploy/cloud/.admin-pending"

ask_admin() {
    # Sets admin_user and admin_password.
    while :; do
        read -rp "    Username (letters, numbers, . _ -): " admin_user
        [ -n "$admin_user" ] && break
    done
    while :; do
        read -rsp "    Password (at least 12 characters): " admin_password; echo
        read -rsp "    Type it again: " p2; echo
        if [ "$admin_password" = "$p2" ] && [ "${#admin_password}" -ge 12 ]; then break; fi
        warn "Those didn't match, or were shorter than 12 characters. Try again."
    done
    unset p2
}

# --- 0. Run in the background, so a dropped connection can't stop it -----------
# The build takes long enough on a small server that a laptop going to sleep,
# or the Wi-Fi blinking, used to kill it halfway through over SSH. So the work
# itself runs detached from this terminal, and this terminal only shows its
# progress: closing it, or losing the connection, leaves the setup running.
# Anything that needs typing is asked here, first, while someone is watching.
if [ -z "${ORCH_SETUP_DETACHED:-}" ]; then
    # One at a time: two runs at once fight over the same containers.
    if ! flock -n "$LOCK" true 2>/dev/null; then
        die "a setup is already running. Watch it with:  tail -f $LOG"
    fi

    need_admin=no
    if [ ! -f "$ENV_FILE" ]; then
        need_admin=yes  # first run: nothing exists yet
    elif command -v docker >/dev/null 2>&1; then
        admins="$(sudo docker compose --env-file "$ENV_FILE" -f deploy/compose.yaml -f deploy/cloud/compose.yaml \
            exec -T postgres psql -U orchestrator -d orchestrator -tAc \
            "select count(*) from users where role = 'ADMIN' and disabled_at is null" 2>/dev/null | tr -d '[:space:]')"
        [ "$admins" = "0" ] && need_admin=yes
    fi
    if [ "$need_admin" = yes ]; then
        say "First, choose your admin account (you sign in with this)."
        ask_admin
        # Handed to the background run in a file only this user can read; it
        # reads and deletes it before doing anything else.
        ( umask 077; printf '%s\n%s\n' "$admin_user" "$admin_password" > "$ADMIN_PENDING" )
        unset admin_password
    fi

    rm -f "$LOG.status"
    ORCH_SETUP_DETACHED=1 setsid nohup bash "$REPO/deploy/cloud/setup.sh" "$@" > "$LOG" 2>&1 < /dev/null &
    worker=$!
    say "Setting up in the background -- you can close this window at any time;"
    say "it keeps going. To watch it again later:  tail -f $LOG"
    echo
    tail -n +1 -f --pid="$worker" "$LOG" 2>/dev/null || true
    exit "$(cat "$LOG.status" 2>/dev/null || echo 1)"
fi

# From here on: the detached run.
trap 'echo $? > "$LOG.status"' EXIT
exec 9>"$LOCK"
flock -n 9 || die "a setup is already running. Watch it with:  tail -f $LOG"

admin_user=""
admin_password=""
if [ -f "$ADMIN_PENDING" ]; then
    { read -r admin_user; read -r admin_password; } < "$ADMIN_PENDING"
    rm -f "$ADMIN_PENDING"
fi

# --- 1. Swap and Docker -----------------------------------------------------------
# The free student server has 1 GB of memory: enough to run everything, not
# enough to build the dashboard or absorb a busy moment without the kernel
# killing something. Disk used as overflow memory makes the difference, and
# costs nothing on a server this size. Added once; kept across reboots.
mem_mb="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
if [ "$mem_mb" -lt 3500 ] && ! swapon --show | grep -q '/swapfile'; then
    say "Adding 4 GB of swap (this server has ${mem_mb} MB of memory)..."
    sudo fallocate -l 4G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=4096
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    ok "Swap added"
fi

# --- Docker -----------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    say "Installing Docker (a few minutes, once)..."
    curl -fsSL https://get.docker.com | sudo sh
fi
# Through sudo for the rest of this run: a group added just now only applies
# to new logins.
DOCKER="sudo docker"
$DOCKER compose version >/dev/null 2>&1 || die "Docker Compose is missing; re-run after: curl -fsSL https://get.docker.com | sudo sh"
ok "Docker ready"

compose() {
    $DOCKER compose --env-file "$ENV_FILE" -f deploy/compose.yaml -f deploy/cloud/compose.yaml "$@"
}

# --- 2. The server's own firewall ------------------------------------------------
# Oracle's Ubuntu images ship iptables rules that reject everything except SSH,
# *in addition to* the cloud network's rules (docs/DEPLOY-ORACLE.md, step 3).
# Both must allow 80 and 443; this handles the server's half. On Azure the
# server firewall is already open and this changes nothing that matters.
if command -v iptables >/dev/null 2>&1; then
    for port in 80 443; do
        sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null \
            || sudo iptables -I INPUT 1 -p tcp --dport "$port" -j ACCEPT
    done
    sudo iptables -C INPUT -p udp --dport 443 -j ACCEPT 2>/dev/null \
        || sudo iptables -I INPUT 1 -p udp --dport 443 -j ACCEPT
    if command -v netfilter-persistent >/dev/null 2>&1; then
        sudo netfilter-persistent save >/dev/null 2>&1 || true
    fi
    ok "Web ports open in the server firewall"
fi

# --- 3. Permanent addresses ------------------------------------------------------
# sslip.io turns an IP address into a web name (1.2.3.4 -> 1.2.3.4.sslip.io)
# for free, with no account, so certificates can be issued without buying a
# domain. Set PUBLIC_HOST to use a domain of your own instead.
PUBLIC_IP="${PUBLIC_IP:-$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')}"
[ -n "$PUBLIC_IP" ] || die "could not work out this server's public IP; set PUBLIC_IP=... and re-run."
PUBLIC_HOST="${PUBLIC_HOST:-${PUBLIC_IP}.sslip.io}"
APP_HOST="app.${PUBLIC_HOST}"
API_HOST="api.${PUBLIC_HOST}"

# --- 4. Settings and secrets -----------------------------------------------------
secret() { openssl rand -hex 32; }
if [ ! -f "$ENV_FILE" ]; then
    say "Writing $ENV_FILE with new secrets (kept for every later run)..."
    umask 077
    cat > "$ENV_FILE" <<EOF
# Generated by deploy/cloud/setup.sh. Contains secrets: never share or commit.
APP_HOST=${APP_HOST}
API_HOST=${API_HOST}
POSTGRES_PASSWORD=$(secret)
MINIO_ROOT_USER=orchestrator
MINIO_ROOT_PASSWORD=$(secret)
JWT_SIGNING_KEY=$(secret)
ADMIN_API_KEY=$(secret)
# The trainer peers pull and run; published on Docker Hub.
TRAINER_IMAGE=abhisheks1290/gpu-orchestrator-trainer:latest
SCHEDULER_STRATEGY=adaptive
# Anyone with the dashboard link can create an account. Set to false to
# create accounts yourself on the People page instead.
ALLOW_REGISTRATION=true
EOF
    ok "Settings written"
else
    # The server's IP can change if it was not reserved; keep the names in
    # step with it, and leave every other setting alone.
    sed -i "s|^APP_HOST=.*|APP_HOST=${APP_HOST}|; s|^API_HOST=.*|API_HOST=${API_HOST}|" "$ENV_FILE"
    ok "Keeping existing settings in $ENV_FILE"
fi

# --- 5. Build and start -----------------------------------------------------------
say "Building and starting (the first time takes 5-10 minutes)..."
compose up -d --build --remove-orphans

say "Waiting for the orchestrator to be ready..."
for _ in $(seq 1 90); do
    if compose exec -T orchestrator curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
compose exec -T orchestrator curl -fsS http://localhost:8000/health >/dev/null 2>&1 \
    || die "the orchestrator did not start. See: sudo docker compose --env-file $ENV_FILE -f deploy/compose.yaml -f deploy/cloud/compose.yaml logs orchestrator"
ok "Orchestrator running"

# --- 6. The first admin account ----------------------------------------------------
admins="$(compose exec -T postgres psql -U orchestrator -d orchestrator -tAc \
    "select count(*) from users where role = 'ADMIN' and disabled_at is null" 2>/dev/null | tr -d '[:space:]')"
if [ "${admins:-0}" = "0" ]; then
    if [ -n "$admin_user" ]; then
        # By environment variable, never as an argument (scripts/create_user.py).
        compose exec -T -e ORCH_USER_PASSWORD="$admin_password" orchestrator \
            python -m scripts.create_user --username "$admin_user" --role ADMIN --no-prompt
        ok "Admin account $admin_user created"
    else
        warn "There is no admin account yet. Run this setup again to create one."
    fi
fi
unset admin_password

# --- 7. Done ---------------------------------------------------------------------
say "Checking the website is reachable over HTTPS (certificates can take a minute)..."
reachable=no
for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null "https://${APP_HOST}/"; then reachable=yes; break; fi
    sleep 4
done

echo
echo "  ====================================================================="
echo "   Website (send this to everyone):   https://${APP_HOST}"
echo "   Machines join through:             https://${API_HOST}"
echo "  ====================================================================="
echo "   These addresses are permanent. Sign in on the website and choose"
echo "   Lend my computer to get the join command for each computer."
echo
if [ "$reachable" = "no" ]; then
    warn "The website isn't reachable from the internet yet. The usual cause is"
    warn "the cloud network's firewall: allow ports 80 and 443 there (Azure:"
    warn "the VM's Networking page; Oracle: the subnet's security list), wait a"
    warn "minute, and run this again."
fi
