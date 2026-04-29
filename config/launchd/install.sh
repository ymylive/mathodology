#!/usr/bin/env bash
# Install Mathodology launchd daemons (macOS). Idempotent — safe to re-run.
#
# Assumes release archive already extracted to /usr/local/mathodology and
# .env populated.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/usr/local/mathodology}"
SVC_USER="${SVC_USER:-_mathodology}"
DAEMONS_DIR="/Library/LaunchDaemons"
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

# ---------- 1. dev token guard ----------
if grep -qE '^DEV_AUTH_TOKEN=dev-local-insecure-token$' "$INSTALL_DIR/.env"; then
  echo "!! DEV_AUTH_TOKEN is still the default 'dev-local-insecure-token'."
  echo "   Set a strong random value in $INSTALL_DIR/.env first."
  echo "   Generate one with: openssl rand -hex 32"
  exit 1
fi

# ---------- 2. service user (idempotent) ----------
# macOS uses dscl, not useradd. Pick the next free UID below 500 (system range).
if ! dscl . -read "/Users/$SVC_USER" >/dev/null 2>&1; then
  echo "==> creating service user $SVC_USER"
  NEXT_UID=$(dscl . -list /Users UniqueID | awk '$2 < 500 {print $2}' | sort -n | tail -1)
  NEXT_UID=$((NEXT_UID + 1))
  dscl . -create "/Users/$SVC_USER"
  dscl . -create "/Users/$SVC_USER" UserShell /usr/bin/false
  dscl . -create "/Users/$SVC_USER" RealName "Mathodology Service"
  dscl . -create "/Users/$SVC_USER" UniqueID "$NEXT_UID"
  dscl . -create "/Users/$SVC_USER" PrimaryGroupID 20
  dscl . -create "/Users/$SVC_USER" NFSHomeDirectory "/var/empty"
else
  echo "==> user $SVC_USER already exists, skipping"
fi

# ---------- 3. directories + ownership ----------
mkdir -p "$INSTALL_DIR/logs" "$INSTALL_DIR/runs" "$INSTALL_DIR/bin"
chown -R "$SVC_USER:staff" "$INSTALL_DIR"

# ---------- 4. install wrapper script ----------
# Wrapper sources .env then execs the right process. Keeps .env as single
# source of truth (vs. inlining env vars into the plist at install time).
WRAPPER="$INSTALL_DIR/bin/launchd-exec.sh"
cat > "$WRAPPER" <<'WRAP'
#!/usr/bin/env bash
# Sourced by launchd. Loads .env then execs gateway or worker.
set -euo pipefail
INSTALL_DIR="/usr/local/mathodology"
cd "$INSTALL_DIR"

set -a
# shellcheck disable=SC1091
. <(tr -d '\r' < "$INSTALL_DIR/.env")
set +a

# Defaults (mirror release-run.sh).
: "${RUNS_DIR:=$INSTALL_DIR/runs}"
: "${STATIC_DIR:=$INSTALL_DIR/apps/web/dist}"
: "${GATEWAY_HOST:=127.0.0.1}"
: "${GATEWAY_PORT:=8080}"
: "${GATEWAY_HTTP:=http://${GATEWAY_HOST}:${GATEWAY_PORT}}"
export RUNS_DIR STATIC_DIR GATEWAY_HOST GATEWAY_PORT GATEWAY_HTTP

# Make uv discoverable. Homebrew installs to /opt/homebrew (Apple silicon)
# or /usr/local (Intel); user installs land in ~/.local/bin.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"

case "${1:-}" in
  gateway)
    exec "$INSTALL_DIR/gateway"
    ;;
  worker)
    cd "$INSTALL_DIR/apps/agent-worker"
    exec uv run python -m agent_worker
    ;;
  *)
    echo "!! usage: $0 {gateway|worker}" >&2
    exit 64
    ;;
esac
WRAP
chmod 0755 "$WRAPPER"
chown "$SVC_USER:staff" "$WRAPPER"

# ---------- 5. install plists ----------
echo "==> installing plists to $DAEMONS_DIR"
install -m 0644 -o root -g wheel "$HERE/com.mathodology.gateway.plist" "$DAEMONS_DIR/"
install -m 0644 -o root -g wheel "$HERE/com.mathodology.worker.plist"  "$DAEMONS_DIR/"

# ---------- 6. (re)bootstrap ----------
# bootout is a no-op if the unit isn't loaded; ignore failure to stay idempotent.
launchctl bootout system "$DAEMONS_DIR/com.mathodology.gateway.plist" 2>/dev/null || true
launchctl bootout system "$DAEMONS_DIR/com.mathodology.worker.plist"  2>/dev/null || true

launchctl bootstrap system "$DAEMONS_DIR/com.mathodology.gateway.plist"
launchctl bootstrap system "$DAEMONS_DIR/com.mathodology.worker.plist"

# ---------- 7. status hint ----------
cat <<EOF

Installed. Verify:
  sudo launchctl print system/com.mathodology.gateway | head -20
  sudo launchctl print system/com.mathodology.worker  | head -20
  tail -f $INSTALL_DIR/logs/gateway.log
  tail -f $INSTALL_DIR/logs/worker.log
  curl -s http://127.0.0.1:8080/health

Uninstall:
  sudo launchctl bootout system $DAEMONS_DIR/com.mathodology.gateway.plist
  sudo launchctl bootout system $DAEMONS_DIR/com.mathodology.worker.plist
  sudo rm $DAEMONS_DIR/com.mathodology.gateway.plist $DAEMONS_DIR/com.mathodology.worker.plist
EOF
