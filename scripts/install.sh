#!/usr/bin/env bash
# install.sh — first-time machine setup for Mathodology (Linux / macOS).
#
# Companion to scripts/preflight.sh: same tool list, same install hints, but
# this script actually runs the install commands. Idempotent — safe to re-run.
#
# Usage:
#   scripts/install.sh                # interactive, runtime tools only
#   scripts/install.sh --yes          # no prompt (CI / scripted)
#   scripts/install.sh --with-source  # also install node + pnpm (for SOURCE builds)
#
# Release-archive users do NOT need --with-source; the gateway binary and
# prebuilt SPA ship in the archive.
set -euo pipefail

ASSUME_YES=0
WITH_SOURCE=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes)        ASSUME_YES=1 ;;
    --with-source)   WITH_SOURCE=1 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown flag: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# ---------- 1. detect platform + package manager ----------
PLATFORM="unknown"; PM=""
case "$(uname -s)" in
  Darwin) PLATFORM="macos"; PM="brew" ;;
  Linux)
    if   command -v apt-get >/dev/null 2>&1; then PLATFORM="debian"; PM="apt"
    elif command -v dnf     >/dev/null 2>&1; then PLATFORM="fedora"; PM="dnf"
    elif command -v pacman  >/dev/null 2>&1; then PLATFORM="arch";   PM="pacman"
    elif command -v zypper  >/dev/null 2>&1; then PLATFORM="suse";   PM="zypper"
    elif command -v apk     >/dev/null 2>&1; then PLATFORM="alpine"; PM="apk"
    else
      echo "!! Linux detected but no known package manager (apt/dnf/pacman/zypper/apk)." >&2
      echo "   See docs/install/linux.md for manual steps." >&2
      exit 1
    fi
    ;;
  *) echo "!! unsupported platform: $(uname -s). Use install.ps1 on Windows." >&2; exit 1 ;;
esac

# brew on macOS is mandatory; bootstrap if missing.
if [ "$PM" = "brew" ] && ! command -v brew >/dev/null 2>&1; then
  echo "!! Homebrew not found. Install it first:"
  echo "   /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  exit 1
fi

INSTALLED=()
SKIPPED=()
FAILED=()

have() { command -v "$1" >/dev/null 2>&1; }

# Wraps install commands so a single failure doesn't kill the whole script.
try_install() {
  # try_install <pretty-name> <cmd...>
  local name="$1"; shift
  if "$@"; then
    INSTALLED+=("$name")
  else
    FAILED+=("$name (cmd: $*)")
  fi
}

# ---------- 2. plan + confirm ----------
plan=()
have python3.11 || have python3 || plan+=("python 3.11")
have uv          || plan+=("uv")
have redis-cli   || plan+=("redis")
have psql        || plan+=("postgresql 14+")
have pandoc      || plan+=("pandoc")
have tectonic    || plan+=("tectonic (optional, for PDF export)")
if [ "$WITH_SOURCE" = "1" ]; then
  have node || plan+=("node 20")
  have pnpm || plan+=("pnpm 9")
fi
have open-websearch || plan+=("open-websearch (optional MCP search)")

echo "Mathodology install ($PLATFORM, pkg manager: $PM)"
echo "------------------------------------------------------------"
if [ "${#plan[@]}" -eq 0 ]; then
  echo "Everything is already installed. Will only verify Postgres/Redis state."
else
  echo "Will install:"
  for p in "${plan[@]}"; do echo "  - $p"; done
fi
echo "Will then: start Redis service, create Postgres role+db (mm/mm/mm if missing)."
[ "$WITH_SOURCE" = "0" ] && echo "Skipping node/pnpm (pass --with-source to include them)."
echo "------------------------------------------------------------"

if [ "$ASSUME_YES" = "0" ]; then
  printf "Proceed? [Y/n] "
  read -r reply </dev/tty || reply=""
  case "$reply" in
    n|N|no|NO) echo "aborted."; exit 0 ;;
  esac
fi

# ---------- 3. installers ----------
# Each block: detect → install via $PM → record outcome. No-op if already present.

install_python() {
  if have python3.11 || have python3; then SKIPPED+=("python"); return; fi
  case "$PM" in
    brew)   try_install python   brew install python@3.11 ;;
    apt)    sudo apt-get update -qq >/dev/null 2>&1 || true
            try_install python   sudo apt-get install -y python3.11 python3.11-venv ;;
    dnf)    try_install python   sudo dnf install -y python3.11 ;;
    pacman) try_install python   sudo pacman -S --needed --noconfirm python ;;
    zypper) try_install python   sudo zypper --non-interactive install python311 ;;
    apk)    try_install python   sudo apk add --no-cache python3 ;;
  esac
}

install_uv() {
  if have uv; then SKIPPED+=("uv"); return; fi
  # uv has no native pkg in apt/dnf — astral.sh installer is the canonical path.
  case "$PM" in
    brew)   try_install uv brew install uv ;;
    *)      try_install uv sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' ;;
  esac
}

install_redis() {
  if have redis-cli; then SKIPPED+=("redis"); return; fi
  case "$PM" in
    brew)   try_install redis brew install redis ;;
    apt)    try_install redis sudo apt-get install -y redis-server ;;
    dnf)    try_install redis sudo dnf install -y redis ;;
    pacman) try_install redis sudo pacman -S --needed --noconfirm redis ;;
    zypper) try_install redis sudo zypper --non-interactive install redis ;;
    apk)    try_install redis sudo apk add --no-cache redis ;;
  esac
}

start_redis() {
  case "$PLATFORM" in
    macos)  brew services start redis >/dev/null 2>&1 || true ;;
    debian) sudo systemctl enable --now redis-server >/dev/null 2>&1 || true ;;
    fedora|arch|suse) sudo systemctl enable --now redis >/dev/null 2>&1 || true ;;
    alpine) sudo rc-update add redis default >/dev/null 2>&1 || true
            sudo rc-service redis start      >/dev/null 2>&1 || true ;;
  esac
}

install_postgres() {
  if have psql; then SKIPPED+=("postgresql"); return; fi
  case "$PM" in
    brew)   try_install postgresql brew install postgresql@16 ;;
    apt)    try_install postgresql sudo apt-get install -y postgresql postgresql-client ;;
    dnf)    try_install postgresql bash -c 'sudo dnf install -y postgresql-server postgresql && sudo postgresql-setup --initdb || true' ;;
    pacman) try_install postgresql sudo pacman -S --needed --noconfirm postgresql ;;
    zypper) try_install postgresql sudo zypper --non-interactive install postgresql postgresql-server ;;
    apk)    try_install postgresql sudo apk add --no-cache postgresql postgresql-client ;;
  esac
}

start_postgres() {
  case "$PLATFORM" in
    macos)  brew services start postgresql@16 >/dev/null 2>&1 \
              || brew services start postgresql >/dev/null 2>&1 || true ;;
    debian|fedora|suse) sudo systemctl enable --now postgresql >/dev/null 2>&1 || true ;;
    arch)   sudo systemctl enable --now postgresql >/dev/null 2>&1 || true ;;
    alpine) sudo rc-update add postgresql default >/dev/null 2>&1 || true
            sudo rc-service postgresql start      >/dev/null 2>&1 || true ;;
  esac
}

# Create role+db (mm/mm/mm) only if absent. Distinguishes brew (current user is
# superuser) from sudo-postgres setups on Linux. Failure here is non-fatal —
# we print exact remediation.
setup_postgres_db() {
  if ! have psql; then
    FAILED+=("postgres db setup (psql missing)")
    return
  fi
  # Wait briefly for the server to accept connections.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if pg_isready -q 2>/dev/null; then break; fi
    sleep 1
  done

  local PSQL_AS_SUPER
  if [ "$PLATFORM" = "macos" ]; then
    PSQL_AS_SUPER="psql -d postgres"
  else
    PSQL_AS_SUPER="sudo -u postgres psql"
  fi

  local has_role has_db
  has_role="$($PSQL_AS_SUPER -tAc "SELECT 1 FROM pg_roles WHERE rolname='mm'" 2>/dev/null || true)"
  has_db="$($PSQL_AS_SUPER   -tAc "SELECT 1 FROM pg_database WHERE datname='mm'" 2>/dev/null || true)"

  if [ "$has_role" != "1" ]; then
    if $PSQL_AS_SUPER -c "CREATE ROLE mm WITH LOGIN PASSWORD 'mm';" >/dev/null 2>&1; then
      INSTALLED+=("postgres role 'mm'")
    else
      FAILED+=("postgres role 'mm' — run manually: $PSQL_AS_SUPER -c \"CREATE ROLE mm WITH LOGIN PASSWORD 'mm';\"")
      return
    fi
  else
    SKIPPED+=("postgres role 'mm' (exists)")
  fi

  if [ "$has_db" != "1" ]; then
    if $PSQL_AS_SUPER -c "CREATE DATABASE mm OWNER mm;" >/dev/null 2>&1; then
      INSTALLED+=("postgres database 'mm'")
    else
      FAILED+=("postgres database 'mm' — run manually: $PSQL_AS_SUPER -c \"CREATE DATABASE mm OWNER mm;\"")
    fi
  else
    SKIPPED+=("postgres database 'mm' (exists)")
  fi
}

install_pandoc() {
  if have pandoc; then SKIPPED+=("pandoc"); return; fi
  case "$PM" in
    brew)   try_install pandoc brew install pandoc ;;
    apt)    try_install pandoc sudo apt-get install -y pandoc ;;
    dnf)    try_install pandoc sudo dnf install -y pandoc ;;
    pacman) try_install pandoc sudo pacman -S --needed --noconfirm pandoc ;;
    zypper) try_install pandoc sudo zypper --non-interactive install pandoc ;;
    apk)    try_install pandoc sudo apk add --no-cache pandoc ;;
  esac
}

# Tectonic is optional. Don't fail the whole run if the distro doesn't ship it.
install_tectonic() {
  if have tectonic; then SKIPPED+=("tectonic"); return; fi
  case "$PM" in
    brew)   try_install tectonic brew install tectonic ;;
    apt)    sudo apt-get install -y tectonic >/dev/null 2>&1 \
              && INSTALLED+=("tectonic") \
              || { SKIPPED+=("tectonic (no apt package; PDF export disabled — install via 'cargo install tectonic' if needed)"); } ;;
    dnf)    sudo dnf install -y tectonic >/dev/null 2>&1 \
              && INSTALLED+=("tectonic") \
              || SKIPPED+=("tectonic (no dnf package; install via 'cargo install tectonic')") ;;
    pacman) try_install tectonic sudo pacman -S --needed --noconfirm tectonic ;;
    zypper) sudo zypper --non-interactive install tectonic >/dev/null 2>&1 \
              && INSTALLED+=("tectonic") \
              || SKIPPED+=("tectonic (no zypper package; install via 'cargo install tectonic')") ;;
    apk)    SKIPPED+=("tectonic (no apk package; install via 'cargo install tectonic')") ;;
  esac
}

install_node_pnpm() {
  if have node; then
    SKIPPED+=("node")
  else
    case "$PM" in
      brew)   try_install node brew install node ;;
      apt)    try_install node sh -c 'curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash - && sudo apt-get install -y nodejs' ;;
      dnf)    try_install node sudo dnf install -y nodejs ;;
      pacman) try_install node sudo pacman -S --needed --noconfirm nodejs npm ;;
      zypper) try_install node sudo zypper --non-interactive install nodejs20 npm20 ;;
      apk)    try_install node sudo apk add --no-cache nodejs npm ;;
    esac
  fi
  if have pnpm; then
    SKIPPED+=("pnpm")
  else
    # corepack ships with node 20+; this is the recommended path.
    try_install pnpm sh -c 'corepack enable && corepack prepare pnpm@latest --activate'
  fi
}

install_open_websearch() {
  if have open-websearch; then SKIPPED+=("open-websearch"); return; fi
  if ! have npm; then
    SKIPPED+=("open-websearch (npm not installed; pass --with-source or install node first)")
    return
  fi
  if npm i -g open-websearch >/dev/null 2>&1; then
    INSTALLED+=("open-websearch")
  else
    SKIPPED+=("open-websearch (npm install failed; optional — try 'sudo npm i -g open-websearch')")
  fi
}

# ---------- 4. run ----------
install_python
install_uv
install_redis;     start_redis
install_postgres;  start_postgres;  setup_postgres_db
install_pandoc
install_tectonic
[ "$WITH_SOURCE" = "1" ] && install_node_pnpm
install_open_websearch

# ---------- 5. summary ----------
echo
echo "------------------------------------------------------------"
echo "Install summary"
echo "------------------------------------------------------------"
if [ "${#INSTALLED[@]}" -gt 0 ]; then
  echo "Installed:"
  for x in "${INSTALLED[@]}"; do echo "  + $x"; done
fi
if [ "${#SKIPPED[@]}" -gt 0 ]; then
  echo "Skipped (already present or optional):"
  for x in "${SKIPPED[@]}"; do echo "  . $x"; done
fi
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "Failed (action required):"
  for x in "${FAILED[@]}"; do echo "  ! $x"; done
  echo
  echo "See docs/install/${PLATFORM}.md for manual steps."
fi
echo
echo "Next: run scripts/preflight.sh to verify, then ./run.sh to start Mathodology."
[ "${#FAILED[@]}" -eq 0 ] || exit 1
exit 0
