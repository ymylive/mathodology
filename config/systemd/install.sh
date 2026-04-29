#!/usr/bin/env bash
# Install Mathodology systemd units. Idempotent — safe to re-run.
#
# Assumes release archive already extracted to /opt/mathodology and .env
# populated (DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY etc.).
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/mathodology}"
SVC_USER="${SVC_USER:-mathodology}"
UNIT_DIR="/etc/systemd/system"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "!! must run as root (sudo $0)" >&2
  exit 1
fi

if [ ! -f "$INSTALL_DIR/gateway" ]; then
  echo "!! $INSTALL_DIR/gateway missing — extract release archive first." >&2
  exit 1
fi
if [ ! -f "$INSTALL_DIR/.env" ]; then
  echo "!! $INSTALL_DIR/.env missing — copy .env.example and fill in keys." >&2
  exit 1
fi

# ---------- 1. service user (idempotent) ----------
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  echo "==> creating system user $SVC_USER"
  useradd -r -m -d "/home/$SVC_USER" -s /usr/sbin/nologin "$SVC_USER"
else
  echo "==> user $SVC_USER already exists, skipping"
fi

# ---------- 2. dev token guard ----------
# Refuse to start in production with the default insecure dev token.
if grep -qE '^DEV_AUTH_TOKEN=dev-local-insecure-token$' "$INSTALL_DIR/.env"; then
  echo "!! DEV_AUTH_TOKEN is still the default 'dev-local-insecure-token'."
  echo "   Set a strong random value in $INSTALL_DIR/.env before enabling the units."
  echo "   Generate one with: openssl rand -hex 32"
  exit 1
fi

# ---------- 3. directories + ownership ----------
mkdir -p "$INSTALL_DIR/runs"
chown -R "$SVC_USER:$SVC_USER" "$INSTALL_DIR"

# ---------- 4. install units ----------
echo "==> installing units to $UNIT_DIR"
install -m 0644 "$HERE/mathodology-gateway.service" "$UNIT_DIR/"
install -m 0644 "$HERE/mathodology-worker.service"  "$UNIT_DIR/"
install -m 0644 "$HERE/mathodology.target"          "$UNIT_DIR/"

systemctl daemon-reload

# ---------- 5. verify uv on PATH for the service user ----------
if ! sudo -u "$SVC_USER" -i bash -lc 'command -v uv' >/dev/null 2>&1; then
  echo "!! uv not on PATH for $SVC_USER. Install with:"
  echo "     sudo -u $SVC_USER bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'"
  echo "   (or install system-wide to /usr/local/bin)"
  echo "   Continuing — worker will fail until uv is available."
fi

# ---------- 6. enable + start ----------
echo "==> enabling + starting mathodology.target"
systemctl enable --now mathodology.target

# ---------- 7. status hint ----------
cat <<EOF

Installed. Verify:
  systemctl status mathodology-gateway.service mathodology-worker.service
  journalctl -u mathodology-gateway.service -f
  journalctl -u mathodology-worker.service  -f
  curl -s http://127.0.0.1:8080/health

Restart both:    sudo systemctl restart mathodology.target
Disable + stop:  sudo systemctl disable --now mathodology.target
EOF
