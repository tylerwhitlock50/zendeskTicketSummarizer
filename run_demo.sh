#!/usr/bin/env bash
# Run a password-protected HTTPS DEMO of the repair portal on a server (e.g. a
# throwaway DigitalOcean droplet). Uses a fake Zendesk: nothing is sent to
# Zendesk or any customer, and your real .env is never read.
#
#   sudo ./run_demo.sh                    start (or restart) the demo over HTTPS
#   sudo ./run_demo.sh --domain NAME      use your own domain (DNS must point here)
#   sudo ./run_demo.sh --http             plain HTTP instead (password sent unencrypted)
#   sudo ./run_demo.sh status             show the address and whether it answers
#   sudo ./run_demo.sh logs               follow the logs
#   sudo ./run_demo.sh stop               stop it; the demo database is deleted
#   sudo ./run_demo.sh purge              stop it and also delete the saved certificate
#
# Environment options (put them AFTER sudo: sudo DEMO_PASSWORD=... ./run_demo.sh):
#   DEMO_PASSWORD     skip the password prompt
#   DEMO_DOMAIN       same as --domain
#   DEMO_HTTPS=0      same as --http
#   DEMO_PUBLIC_PORT  port for plain HTTP (default 80)
#
# What it does:
#   - installs Docker + Compose v2 if missing (apt)
#   - asks for a demo password (or generates one); every visitor must enter it
#   - firewall (ufw): deny incoming except SSH, 80 and 443; fail2ban for SSH
#   - starts the demo + its own throwaway database (never published), with
#     Caddy in front getting a real Let's Encrypt certificate for
#     <ip-with-dashes>.sslip.io (a free name that points at this server)
#   - waits until the site answers, then prints the address
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi   # "sh run_demo.sh" works too
set -euo pipefail
cd "$(dirname "$0")"

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a   # no hidden apt prompts

ENV_FILE=".env.demo"
PROJECT="rma-demo"
SELF="./$(basename "$0")"   # how to run it again from this folder, for the hints

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- arguments
CMD="start"
HTTPS="${DEMO_HTTPS:-1}"
DOMAIN_ARG="${DEMO_DOMAIN:-}"
while [ $# -gt 0 ]; do
    case "$1" in
        start | stop | purge | status | logs) CMD="$1" ;;
        --http) HTTPS=0 ;;
        --https) HTTPS=1 ;;
        --domain)
            [ $# -ge 2 ] || die "--domain needs a name, e.g. --domain demo.example.com"
            DOMAIN_ARG="$2"; shift ;;
        --domain=*) DOMAIN_ARG="${1#--domain=}" ;;
        -h | --help) sed -n '2,27p' "$SELF"; exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
    shift
done
[ "$HTTPS" = "0" ] || HTTPS=1
[ "$(id -u)" -eq 0 ] || die "Run with sudo: sudo $SELF $CMD"
if [ -n "$DOMAIN_ARG" ] && ! [[ "$DOMAIN_ARG" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
    die "That doesn't look like a domain name: $DOMAIN_ARG"
fi

# ---------------------------------------------------------------- Docker + Compose v2
apt_install() {
    command -v apt-get >/dev/null 2>&1 || return 1
    apt-get update -qq >/dev/null 2>&1 || true
    apt-get install -y -qq "$@" >/dev/null 2>&1
}
if ! command -v docker >/dev/null 2>&1; then
    say "Installing Docker (one time)..."
    apt_install docker.io || die "Couldn't install Docker. Install it, then run this again."
    systemctl enable --now docker >/dev/null 2>&1 || true
fi
# The old docker-compose 1.x breaks on current Docker ("KeyError: 'ContainerConfig'"),
# so use Compose v2 ("docker compose") and install it if needed.
if ! docker compose version >/dev/null 2>&1; then
    say "Installing Docker Compose v2 (one time)..."
    apt_install docker-compose-v2 || apt_install docker-compose-plugin || true
    docker compose version >/dev/null 2>&1 \
        || die "Docker Compose v2 isn't available. Install the docker-compose-v2 (Ubuntu) or docker-compose-plugin package."
fi
# Compose v2 builds with buildx; Ubuntu's docker.io doesn't include it.
if ! docker buildx version >/dev/null 2>&1; then
    apt_install docker-buildx || apt_install docker-buildx-plugin || true
fi

# ---------------------------------------------------------------- helpers
env_get() {  # read one KEY from .env.demo without executing the file
    [ -f "$ENV_FILE" ] || return 0
    grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- || true
}

compose() {
    local files=(-f docker-compose.demo.yml)
    if [ "$HTTPS" = "1" ]; then files+=(-f docker-compose.demo-https.yml); else files+=(-f docker-compose.demo-http.yml); fi
    docker compose -p "$PROJECT" --env-file "$ENV_FILE" "${files[@]}" "$@"
}

# Remove this demo's containers whether or not .env.demo exists (any Compose version).
remove_containers() {
    local ids
    ids="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")"
    [ -z "$ids" ] || docker rm -f $ids >/dev/null
    docker rm -f demo-https >/dev/null 2>&1 || true   # a hand-started Caddy from earlier instructions
}

site_url() {  # $1 https flag, $2 domain, $3 public ip, $4 port
    if [ "$1" = "1" ]; then printf 'https://%s' "$2"
    elif [ "$4" = "80" ]; then printf 'http://%s' "$3"
    else printf 'http://%s:%s' "$3" "$4"; fi
}

# Answers 401 (the password box) once the app is up behind the proxy.
probe() { curl -s -o /dev/null -m 8 -w '%{http_code}' "$1/" 2>/dev/null || true; }

load_running() {  # settings of the running demo, for status/logs
    [ -f "$ENV_FILE" ] || die "The demo isn't running (no $ENV_FILE)."
    HTTPS="$(env_get DEMO_HTTPS_MODE)"; HTTPS="${HTTPS:-1}"
    URL="$(site_url "$HTTPS" "$(env_get DEMO_DOMAIN)" "$(env_get DEMO_PUBLIC_IP)" "$(env_get DEMO_PUBLIC_PORT)")"
}

case "$CMD" in
    stop)
        remove_containers
        rm -f "$ENV_FILE"
        say "Demo stopped; its database is gone. (The HTTPS certificate is kept for a quick restart; 'purge' deletes it.)"
        exit 0 ;;
    purge)
        remove_containers
        docker volume rm "${PROJECT}_caddy_data" >/dev/null 2>&1 || true
        docker network rm "${PROJECT}_default" >/dev/null 2>&1 || true
        rm -f "$ENV_FILE"
        say "Demo and saved certificate deleted."
        exit 0 ;;
    logs)
        load_running
        compose logs -f --tail 100
        exit 0 ;;
    status)
        load_running
        say "Address: $URL/incoming   (answers: $(probe "$URL"); 401 = up and asking for the password)"
        compose ps
        exit 0 ;;
esac

# ---------------------------------------------------------------- start
PORT="${DEMO_PUBLIC_PORT:-80}"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "DEMO_PUBLIC_PORT must be a number."

# Public IP: DigitalOcean's metadata service (local to the droplet), else the first address.
PUBLIC_IP="$(curl -s -m 2 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address 2>/dev/null || true)"
[[ "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$PUBLIC_IP" ] || die "Couldn't work out this server's IP address. Use --domain."
DOMAIN="${DOMAIN_ARG:-${PUBLIC_IP//./-}.sslip.io}"

# Password: keep the running demo's password on a restart unless a new one is given.
PASSWORD="${DEMO_PASSWORD:-}"
if [ -z "$PASSWORD" ]; then
    PASSWORD="$(env_get DEMO_PASSWORD)"
    [ -z "$PASSWORD" ] || say "Keeping the current demo password (sudo DEMO_PASSWORD=... $SELF to change it)."
fi
GENERATED=""
if [ -z "$PASSWORD" ]; then
    if [ -t 0 ]; then
        read -r -s -p "Demo password (blank = generate one): " PASSWORD; echo
        if [ -n "$PASSWORD" ]; then
            read -r -s -p "Again: " AGAIN; echo
            [ "$PASSWORD" = "$AGAIN" ] || die "Passwords don't match."
        fi
    fi
    if [ -z "$PASSWORD" ]; then
        PASSWORD="$(tr -dc 'A-HJ-NP-Za-km-z2-9' </dev/urandom | head -c 14 || true)"
        GENERATED=1
    fi
fi
[ "${#PASSWORD}" -ge 8 ] || die "Use a password of at least 8 characters."
[[ "$PASSWORD" =~ ^[A-Za-z0-9._@%+=:,/!?#~^*-]+$ ]] \
    || die "Use letters, numbers and these symbols only: . _ @ % + = : , / ! ? # ~ ^ * -"

umask 077
{
    echo "DEMO_PASSWORD=$PASSWORD"
    echo "DEMO_DB_PASSWORD=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 || true)"
    echo "DEMO_SECRET_KEY=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48 || true)"
    echo "DEMO_DOMAIN=$DOMAIN"
    echo "DEMO_PUBLIC_IP=$PUBLIC_IP"
    echo "DEMO_PUBLIC_PORT=$PORT"
    echo "DEMO_HTTPS_MODE=$HTTPS"
} >"$ENV_FILE"

# ---------------------------------------------------------------- firewall + fail2ban
# SSH ports: whatever sshd actually listens on (sudo hides SSH_CONNECTION), plus 22.
ssh_ports() {
    {
        echo 22
        echo "${SSH_CONNECTION:-}" | awk 'NF>=4 {print $4}'
        sshd -T 2>/dev/null | awk '$1=="port" {print $2}'
        ss -ltnpH 2>/dev/null | awk '/"sshd"|systemd/ {n=split($4,a,":"); print a[n]}'
        systemctl cat ssh.socket 2>/dev/null | awk -F'[=:]' '/^ListenStream/ {print $NF}'
    } | grep -E '^[0-9]+$' | sort -un
}
command -v ufw >/dev/null 2>&1 || apt_install ufw || true
setup_firewall() {
    local p
    for p in $(ssh_ports); do   # SSH first, so enabling can't cut you off
        ufw allow "$p"/tcp >/dev/null
        ufw limit "$p"/tcp >/dev/null   # slow down SSH password guessing
    done
    if [ "$HTTPS" = "1" ]; then
        ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null && ufw allow 443/udp >/dev/null
    else
        ufw allow "$PORT"/tcp >/dev/null
    fi
    ufw default deny incoming >/dev/null && ufw default allow outgoing >/dev/null && ufw --force enable >/dev/null
}
if command -v ufw >/dev/null 2>&1; then
    if setup_firewall; then
        say "Firewall on. Open incoming ports:"
        ufw status | awk '/ALLOW|LIMIT/ {print "  " $SELF}' || true
    else
        say "WARNING: couldn't set up the ufw firewall; use the droplet's Cloud Firewall instead."
    fi
else
    say "WARNING: ufw isn't available; use the droplet's Cloud Firewall instead."
fi
if ! systemctl is-active --quiet fail2ban 2>/dev/null; then
    apt_install fail2ban && systemctl enable --now fail2ban >/dev/null 2>&1 || true
fi
if systemctl is-active --quiet fail2ban 2>/dev/null; then
    say "fail2ban is protecting SSH."
else
    say "Note: fail2ban isn't running (SSH is still rate-limited by the firewall)."
fi

# ---------------------------------------------------------------- free ports, start
# Stop the regular app and any earlier demo so the ports are free. Containers are
# removed rather than recreated (avoids old-Compose recreate bugs).
docker compose -f docker-compose.yml down >/dev/null 2>&1 || true
docker compose -f docker-compose.yml -f docker-compose.dev.yml down >/dev/null 2>&1 || true
if command -v docker-compose >/dev/null 2>&1; then docker-compose -f docker-compose.yml down >/dev/null 2>&1 || true; fi
remove_containers
if [ "$HTTPS" = "1" ]; then want='80|443'; else want="$PORT"; fi
busy="$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ":($want)\$" || true)"
[ -z "$busy" ] || die "Port $(echo "$busy" | tr '\n' ' ')is already in use by another program. Stop it and try again."

say "Building and starting the demo (the first run takes a few minutes)..."
compose up -d --build

URL="$(site_url "$HTTPS" "$DOMAIN" "$PUBLIC_IP" "$PORT")"
if [ "$HTTPS" = "1" ]; then say "Waiting for $URL (getting the HTTPS certificate)..."; else say "Waiting for $URL..."; fi
code=""
for _ in $(seq 1 60); do
    code="$(probe "$URL")"
    [ "$code" = "401" ] && break
    sleep 3
done

say ""
if [ "$code" = "401" ]; then
    say "Demo is up:  $URL/incoming"
else
    say "The demo didn't answer at $URL yet (last response: ${code:-none}). Recent logs:"
    compose logs --tail 25 2>&1 | sed 's/^/  /' || true
    say ""
    say "Common causes: the droplet's Cloud Firewall blocks 80/443; Let's Encrypt is busy"
    say "(wait a minute, then: sudo $SELF status); or try plain HTTP: sudo $SELF --http"
fi
if [ -n "$GENERATED" ]; then
    say "  Browser password:  $PASSWORD   (generated; any user name)"
else
    say "  Browser password:  the one you entered (any user name)"
fi
say "  Then sign in as:   Demo Tech, PIN 1234"
say "  Status: sudo $SELF status   Logs: sudo $SELF logs   Stop: sudo $SELF stop"
