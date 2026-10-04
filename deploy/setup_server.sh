#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 22.04/24.04 VPS for Cheapest Trip. Run as root:
#   bash setup_server.sh <site-host>        e.g. 185-1-2-3.sslip.io  (sslip.io maps it to IP 185.1.2.3)
# Installs Python + Caddy, creates the `cheaptrip` system user, a systemd service on 127.0.0.1:8770
# and Caddy in front of it with automatic HTTPS (Let's Encrypt). The code itself is uploaded by
# deploy_vps.sh; this script never sees any secret.
set -euo pipefail

SITE_HOST="${1:?usage: setup_server.sh <site-host, e.g. 185-1-2-3.sslip.io>}"
APP_DIR=/opt/cheaptrip

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip ufw curl
if ! apt-get install -y -q caddy; then
  # Older Ubuntu without caddy in universe: use Caddy's official repository.
  apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https gnupg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi

id cheaptrip >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin cheaptrip
mkdir -p "$APP_DIR"
[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
chown -R cheaptrip:cheaptrip "$APP_DIR"

cat > /etc/systemd/system/cheaptrip.service <<EOF
[Unit]
Description=Cheapest Trip (flights + hotels price search)
After=network-online.target
Wants=network-online.target

[Service]
User=cheaptrip
Group=cheaptrip
WorkingDirectory=$APP_DIR
Environment=TZ=Europe/Moscow
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/venv/bin/python -m uvicorn cheaptrip.api:app --host 127.0.0.1 --port 8770 --proxy-headers --forwarded-allow-ips 127.0.0.1 --log-level warning
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/caddy/Caddyfile <<EOF
$SITE_HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8770
}

# Plain http://<ip> also works (redirects nowhere, just serves the site).
:80 {
    reverse_proxy 127.0.0.1:8770
}
EOF

ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

systemctl daemon-reload
systemctl enable cheaptrip >/dev/null
systemctl enable caddy >/dev/null
systemctl restart caddy
echo "Server ready. Now upload the code with deploy_vps.sh; site: https://$SITE_HOST"
