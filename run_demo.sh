#!/usr/bin/env bash
# Run a password-protected HTTPS DEMO of the repair portal on a server (e.g. a
# throwaway DigitalOcean droplet). Uses a fake Zendesk: nothing is sent to
# Zendesk or any customer, and your real .env is never read.
#
#   sudo ./run_demo.sh            start (or restart) the demo
#   sudo ./run_demo.sh stop       stop it; the demo database is deleted
#   sudo ./run_demo.sh purge      stop it and also delete the saved certificate
#   sudo ./run_demo.sh status     show the address and whether it answers
#   sudo ./run_demo.sh logs       follow the logs
#
# Options (environment variables):
#   DEMO_PASSWORD     skip the password prompt
#   DEMO_DOMAIN       your own domain pointing at this server (default:
#                     <ip-with-dashes>.sslip.io, a free name for this IP)
#   DEMO_HTTPS=0      plain HTTP instead (password sent unencrypted)
#   DEMO_PUBLIC_PORT  port for plain HTTP (default 80)
#
# What it does:
#   - installs Docker + Compose v2 if missing (apt)
#   - asks for a demo password (or generates one); every visitor must enter it
#   - firewall (ufw): deny all incoming except SSH, 80 and 443; fail2ban for SSH
#   - starts the demo + its own throwaway database (never published), with
#     Caddy in front getting a real Let's Encrypt certificate
#   - waits until https://<name>/ answers, then prints the address
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env.demo"
PROJECT="rma-demo"
CMD="${1:-start}"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

case "$CMD" in start | stop | purge | status | logs) ;; *) die "Usage: $0 [start|stop|purge|status|logs]" ;; esac
[ "$(id -u)" -eq 0 ] || die "Run with sudo: sudo $0 $CMD"

# ---------------------------------------------------------------- Docker + Compose
install_docker() {
    command -v apt-get >/dev/null 2>&1 || die "Docker is missing and apt isn't available. Install Docker first."
    say "Installing Docker (one time)..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq docker.io >/dev/null
    systemctl enable --now docker >/dev/null 2>&1 || true
}
command -v docker >/dev/null 2>&1 || install_docker

# Compose v2 ("docker compose"). The old docker-compose 1.x breaks on current
# Docker ("KeyError: 'ContainerConfig'"), so install v2 rather than use it.
if ! docker compose version >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        say "Installing Docker Compose v2 (one time)..."
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1 \
            || apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || true
    fi
    docker compose version >/dev/null 2>&1 \
        || die "Docker Compose v2 isn't available. Install the docker-compose-v2 (Ubuntu) or docker-compose-plugin package."
fi

compose_files() {
    local files=(-f docker-compose.demo.yml)
    if [ "${HTTPS:-1}" = "1" ]; then files+=(-f docker-compose.demo-https.yml); else files+=(-f docker-compose.demo-http.yml); fi
    printf '%s\n' "${files[@]}"
}
compose() {
    local files
    mapfile -t files < <(compose_files)
    docker compose -p "$PROJECT" --env-file "$ENV_FILE" "${files[@]}" "$@"
}

# Remove this demo's containers whether or not .env.demo exists (works for any Compose version).
remove_containers() {
    local ids
    ids="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")"
    [ -z "$ids" ] || docker rm -f $ids >/dev/null
    docker rm -f demo-https >/dev/null 2>&1 || true   # a hand-started Caddy from earlier instructions
}

read_env() {
    # shellcheck disable=SC1090
    [ -f "$ENV_FILE" ] && . "./$ENV_FILE"
    HTTPS="${DEMO_HTTPS_MODE:-1}"
}

url_for() {
    if [ "$HTTPS" = "1" ]; then printf 'https://%s' "$DEMO_DOMAIN"
    else printf 'http://%s%s' "$PUBLIC_IP" "$([ "${DEMO_PUBLIC_PORT:-80}" = 80 ] || printf ':%s' "$DEMO_PUBLIC_PORT")"; fi
}

# Answers with 401 (the password box) once the app is up behind the proxy.
probe() { curl -s -o /dev/null -m 8 -w '%{http_code}' "$1/" 2>/dev/null || true; }

case "$CMD" in
    stop)
        remove_containers
        rm -f "$ENV_FILE"
        say "Demo stopped; its database is gone. (The HTTPS certificate is kept for a quick restart; use 'purge' to delete it.)"
        exit 0
        ;;
    purge)
        remove_containers
        docker volume rm "${PROJECT}_caddy_data" >/dev/null 2>&1 || true
        docker network rm "${PROJECT}_default" >/dev/null 2>&1 || true
        rm -f "$ENV_FILE"
        say "Demo and saved certificate deleted."
        exit 0
        ;;
    logs)
        [ -f "$ENV_FILE" ] || die "The demo isn't running."
        read_env
        compose logs -f --tail 100
        exit 0
        ;;
    status)
        [ -f "$ENV_FILE" ] || die "The demo isn't running."
        read_env
        URL="$(url_for)"
        say "Address: $URL/incoming   (answers: $(probe "$URL"); 401 = up and asking for the password)"
        compose ps
        exit 0
        ;;
esac

# ---------------------------------------------------------------- start
HTTPS="${DEMO_HTTPS:-1}"
PORT="${DEMO_PUBLIC_PORT:-80}"

# Public IP: DigitalOcean's metadata service (local to the droplet), else the first address.
PUBLIC_IP="$(curl -s -m 2 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address 2>/dev/null || true)"
[[ "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$PUBLIC_IP" ] || die "Couldn't work out this server's IP address. Set DEMO_DOMAIN."
DOMAIN="${DEMO_DOMAIN:-${PUBLIC_IP//./-}.sslip.io}"

# Password: keep the running demo's password on a restart unless a new one is given.
PASSWORD="${DEMO_PASSWORD:-}"
if [ -z "$PASSWORD" ] && [ -f "$ENV_FILE" ]; then
    PASSWORD="$(grep -E '^DEMO_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
    [ -z "$PASSWORD" ] || say "Keeping the current demo password (set DEMO_PASSWORD to change it)."
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
case "$PASSWORD" in *[[:space:]\'\"\$\`\\]*) die "Use a password without spaces, quotes, \$, \` or \\." ;; esac

umask 077
{
    echo "DEMO_PASSWORD=$PASSWORD"
    echo "DEMO_DB_PASSWORD=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 || true)"
    echo "DEMO_SECRET_KEY=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48 || true)"
    echo "DEMO_DOMAIN=$DOMAIN"
    echo "DEMO_PUBLIC_PORT=$PORT"
    echo "DEMO_HTTPS_MODE=$HTTPS"
} >"$ENV_FILE"

# ---------------------------------------------------------------- firewall + fail2ban
SSH_PORT="$(echo "${SSH_CONNECTION:-}" | awk '{print $4}')"
SSH_PORT="${SSH_PORT:-22}"
if ! command -v ufw >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    apt-get install -y -qq ufw >/dev/null 2>&1 || true
fi
if command -v ufw >/dev/null 2>&1; then
    ufw allow "$SSH_PORT"/tcp >/dev/null
    ufw limit "$SSH_PORT"/tcp >/dev/null   # slow down SSH password guessing
    if [ "$HTTPS" = "1" ]; then
        ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 443/udp >/dev/null
    else
        ufw allow "$PORT"/tcp >/dev/null
    fi
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    ufw --force enable >/dev/null
    say "Firewall on: SSH ($SSH_PORT) and the demo ports only."
else
    say "WARNING: ufw isn't available; use the droplet's Cloud Firewall instead."
fi
if ! command -v fail2ban-client >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    apt-get install -y -qq fail2ban >/dev/null 2>&1 && systemctl enable --now fail2ban >/dev/null 2>&1 || true
fi
command -v fail2ban-client >/dev/null 2>&1 && say "fail2ban is protecting SSH."

# ---------------------------------------------------------------- free ports, start
# Stop the regular app and any earlier demo so ports 80/443 are free. Containers
# are removed rather than recreated (avoids old-Compose recreate bugs).
docker compose -f docker-compose.yml down >/dev/null 2>&1 || true
docker compose -f docker-compose.yml -f docker-compose.dev.yml down >/dev/null 2>&1 || true
command -v docker-compose >/dev/null 2>&1 && { docker-compose -f docker-compose.yml down >/dev/null 2>&1 || true; }
remove_containers
busy="$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':(80|443)$' || true)"
if [ "$HTTPS" = "1" ] && [ -n "$busy" ]; then
    die "Port 80 or 443 is already in use by another program ($(echo $busy)). Stop it and try again."
fi

say "Building and starting the demo (first run takes a few minutes)..."
compose up -d --build

URL="$(url_for)"
say "Waiting for $URL to answer$([ "$HTTPS" = 1 ] && printf ' (getting the HTTPS certificate)')..."
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
    compose logs --tail 25 2>&1 | sed 's/^/  /'
    say ""
    say "Common causes: the droplet's Cloud Firewall blocks 80/443; Let's Encrypt is busy"
    say "(wait a minute: sudo $0 status); or plain HTTP is needed: DEMO_HTTPS=0 sudo $0"
fi
if [ -n "$GENERATED" ]; then
    say "  Browser password:  $PASSWORD   (generated; any user name)"
else
    say "  Browser password:  the one you entered (any user name)"
fi
say "  Then sign in as:   Demo Tech, PIN 1234"
say "  Status: sudo $0 status   Logs: sudo $0 logs   Stop: sudo $0 stop"
