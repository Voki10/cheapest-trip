#!/usr/bin/env bash
# Upload the code + .env to the VPS and (re)start the site. Run from the project folder (Git Bash):
#   bash deploy/deploy_vps.sh <server-ip> [ssh-user] [ssh-key]
# ssh-user defaults to root; for clouds that log in as a regular sudo user (Yandex Cloud etc.)
# pass that user's name. First run on a fresh server also performs setup_server.sh.
# Safe to run again for every update.
set -euo pipefail

IP="${1:?usage: deploy/deploy_vps.sh <server-ip> [ssh-user] [ssh-key]}"
SSH_USER="${2:-root}"
KEY="${3:-$HOME/.ssh/cheaptrip_vps}"
SITE_HOST="${IP//./-}.sslip.io"
APP_DIR=/opt/cheaptrip
SUDO=""
[ "$SSH_USER" = "root" ] || SUDO="sudo"

# Fresh cloud VMs sometimes drop a new SSH connection before the handshake; retry those.
# usage: remote <stdin-file|-> <command>
remote() {
  local input="$1"; shift
  for attempt in 1 2 3 4 5 6; do
    if [ "$input" = "-" ]; then
      ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o BatchMode=yes "$SSH_USER@$IP" "$@" </dev/null && return 0
    else
      ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o BatchMode=yes "$SSH_USER@$IP" "$@" <"$input" && return 0
    fi
    code=$?
    [ $code -eq 255 ] || return $code  # the command itself failed: do not retry
    echo "   (ssh connection dropped, retry $attempt)"; sleep 5
  done
  return 255
}

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env in $(pwd)"; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if ! remote - "systemctl cat cheaptrip.service >/dev/null 2>&1 && test -d $APP_DIR/venv"; then
  echo "== first run: preparing the server"
  remote deploy/setup_server.sh "$SUDO bash -s -- $SITE_HOST"
fi

echo "== uploading code (without .env, caches, local database)"
tar czf "$TMP/app.tgz" --exclude=./.env --exclude=./.env.server --exclude=./cheaptrip/data/cache --exclude='*.db' \
    --exclude='*.db-journal' --exclude='*.db-wal' --exclude='*.db-shm' --exclude='__pycache__' \
    --exclude=./.pytest_cache --exclude=./.git .
remote "$TMP/app.tgz" "$SUDO mkdir -p $APP_DIR && $SUDO tar xzf - -C $APP_DIR"

echo "== uploading .env (secrets stay readable only by the service user)"
# The keys for publishing on Render stay on this computer.
grep -vE '^(DEPLOY_GITHUB_TOKEN|RENDER_API_KEY)=' .env > "$TMP/server.env" || true
remote "$TMP/server.env" "$SUDO tee $APP_DIR/.env >/dev/null"

echo "== installing dependencies and restarting"
cat > "$TMP/finish.sh" <<EOF
set -e
# The server got a new public IP (e.g. a stopped VM restarted): point HTTPS at the new sslip.io host.
if ! grep -q "^$SITE_HOST {" /etc/caddy/Caddyfile; then
  sed -i "1s/^.* {\$/$SITE_HOST {/" /etc/caddy/Caddyfile && systemctl reload caddy && echo "caddy: now serving $SITE_HOST"
fi
# This copy's public address: the Telegram bot gets its messages here by webhook.
echo "PUBLIC_URL=https://$SITE_HOST" > $APP_DIR/.env.server
chown -R cheaptrip:cheaptrip $APP_DIR
chmod 600 $APP_DIR/.env
runuser -u cheaptrip -- $APP_DIR/venv/bin/pip install -q --upgrade pip
runuser -u cheaptrip -- $APP_DIR/venv/bin/pip install -q -r $APP_DIR/requirements.txt
systemctl restart cheaptrip
for i in \$(seq 1 90); do curl -fs -o /dev/null http://127.0.0.1:8770/api/status && break; sleep 1; done
systemctl is-active cheaptrip
EOF
remote "$TMP/finish.sh" "$SUDO bash -s"

echo
echo "Site: https://$SITE_HOST   (also http://$IP)"
