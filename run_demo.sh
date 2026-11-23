#!/usr/bin/env bash
# Run a password-protected DEMO of the repair portal on a server (e.g. a
# throwaway DigitalOcean droplet). Uses a fake Zendesk: nothing is sent to
# Zendesk or any customer.
#
#   sudo ./run_demo.sh          set a password, lock down the firewall, start the demo
#   sudo ./run_demo.sh stop     stop the demo and delete its database
#   sudo ./run_demo.sh logs     follow the demo's log
#
# Options (environment): DEMO_PUBLIC_PORT (default 80), DEMO_PASSWORD (skip the prompt).
#
# What it does:
#   - asks for a demo password (or generates one); every visitor must enter it
#   - firewall (ufw): deny all incoming except SSH and the demo port
#   - fail2ban for SSH brute-force protection (installed if apt is available)
#   - starts the demo with its own throwaway database that is NOT published to
#     the internet, and never reads your real .env
#
# Note: the password travels over plain HTTP. Fine for a short demo; for
# anything longer, put it behind HTTPS (e.g. Caddy with a domain).
set -euo pipefail
cd "$(dirname "$0")"

PORT="${DEMO_PUBLIC_PORT:-80}"
ENV_FILE=".env.demo"
PROJECT="rma-demo"

if docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
else
    echo "Docker Compose is not installed." >&2
    exit 1
fi
compose() { "${DC[@]}" -p "$PROJECT" --env-file "$ENV_FILE" -f docker-compose.demo.yml "$@"; }

case "${1:-start}" in
    stop)
        if [ -f "$ENV_FILE" ]; then compose down -v; fi
        rm -f "$ENV_FILE"
        echo "Demo stopped and its database deleted."
        exit 0
        ;;
    logs)
        compose logs -f demo-app
        exit 0
        ;;
    start) ;;
    *)
        echo "Usage: $0 [start|stop|logs]" >&2
        exit 1
        ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo (needed for the firewall): sudo $0" >&2
    exit 1
fi

# ---- password
PASSWORD="${DEMO_PASSWORD:-}"
if [ -z "$PASSWORD" ]; then
    read -r -s -p "Demo password (blank = generate one): " PASSWORD; echo
    if [ -n "$PASSWORD" ]; then
        read -r -s -p "Again: " AGAIN; echo
        [ "$PASSWORD" = "$AGAIN" ] || { echo "Passwords don't match." >&2; exit 1; }
    else
        PASSWORD="$(tr -dc 'A-HJ-NP-Za-km-z2-9' </dev/urandom | head -c 14)"
        GENERATED=1
    fi
fi
if [ "${#PASSWORD}" -lt 8 ]; then
    echo "Use at least 8 characters." >&2
    exit 1
fi

umask 077
cat >"$ENV_FILE" <<EOF
DEMO_PASSWORD=$PASSWORD
DEMO_DB_PASSWORD=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)
DEMO_SECRET_KEY=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48)
DEMO_PUBLIC_PORT=$PORT
EOF

# ---- firewall: keep the SSH port you're connected on, open the demo port, deny the rest
SSH_PORT="$(echo "${SSH_CONNECTION:-}" | awk '{print $4}')"
SSH_PORT="${SSH_PORT:-22}"
if ! command -v ufw >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq ufw >/dev/null
fi
if command -v ufw >/dev/null 2>&1; then
    ufw allow "$SSH_PORT"/tcp >/dev/null
    ufw limit "$SSH_PORT"/tcp >/dev/null   # slow down SSH password guessing
    ufw allow "$PORT"/tcp >/dev/null
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    ufw --force enable >/dev/null
    echo "Firewall on: only SSH ($SSH_PORT) and the demo ($PORT) are open."
else
    echo "WARNING: ufw isn't available; set up the droplet's Cloud Firewall instead." >&2
fi

# ---- fail2ban for SSH
if ! command -v fail2ban-client >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    apt-get install -y -qq fail2ban >/dev/null && systemctl enable --now fail2ban >/dev/null 2>&1 || true
fi
command -v fail2ban-client >/dev/null 2>&1 && echo "fail2ban is protecting SSH."

# ---- free the port if the regular app (or an earlier demo) is running
"${DC[@]}" -f docker-compose.yml down >/dev/null 2>&1 || true
"${DC[@]}" -f docker-compose.yml -f docker-compose.dev.yml down >/dev/null 2>&1 || true

# ---- start
compose up -d --build

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
URL="http://${IP:-<droplet-ip>}$([ "$PORT" = 80 ] || echo ":$PORT")/incoming"
echo
echo "Demo is starting at: $URL"
echo "  Browser password:  ${GENERATED:+$PASSWORD (generated)}${GENERATED:-the one you entered} (any user name)"
echo "  Then sign in as:   Demo Tech, PIN 1234"
echo "Logs: sudo $0 logs    Stop and delete everything: sudo $0 stop"
