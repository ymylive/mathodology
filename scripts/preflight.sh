#!/usr/bin/env bash
# preflight.sh — verify Mathodology runtime prereqs (Linux / macOS).
#
# Returns 0 iff every required tool is present and >= the minimum version.
# Optional tools are reported but do not fail the check.
#
# Output format (one line per tool):
#   [ OK   ] <tool> <version>
#   [ MISS ] <tool> not found        — install: <hint>
#   [ STALE] <tool> <found> < <min>  — upgrade: <hint>
set -uo pipefail

# Detect platform once (used to pick install hint).
PLATFORM="unknown"
case "$(uname -s)" in
  Darwin) PLATFORM="macos" ;;
  Linux)
    if command -v apt-get >/dev/null 2>&1; then PLATFORM="debian"
    elif command -v dnf     >/dev/null 2>&1; then PLATFORM="fedora"
    elif command -v pacman  >/dev/null 2>&1; then PLATFORM="arch"
    elif command -v zypper  >/dev/null 2>&1; then PLATFORM="suse"
    elif command -v apk     >/dev/null 2>&1; then PLATFORM="alpine"
    else PLATFORM="linux"; fi
    ;;
esac

FAIL=0
WARN=0

# Compare two dotted versions: returns 0 iff $1 >= $2.
ver_ge() {
  [ "$1" = "$2" ] && return 0
  printf '%s\n%s\n' "$2" "$1" | sort -C -V
}

# install_hint <tool> <required|optional>
install_hint() {
  local tool="$1" tier="$2"
  case "$tool:$PLATFORM" in
    python3.11:macos|python3:macos)   echo "brew install python@3.11" ;;
    python3.11:debian|python3:debian) echo "sudo apt install -y python3.11 python3.11-venv" ;;
    python3.11:fedora|python3:fedora) echo "sudo dnf install -y python3.11" ;;
    python3.11:arch|python3:arch)     echo "sudo pacman -S --needed python" ;;
    python3.11:alpine|python3:alpine) echo "sudo apk add python3" ;;
    uv:*)                    echo "curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
    node:macos)              echo "brew install node" ;;
    node:debian)             echo "curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash - && sudo apt install -y nodejs" ;;
    node:fedora)             echo "sudo dnf install -y nodejs:20" ;;
    node:arch)               echo "sudo pacman -S --needed nodejs npm" ;;
    node:alpine)             echo "sudo apk add nodejs npm" ;;
    pnpm:*)                  echo "corepack enable && corepack prepare pnpm@latest --activate" ;;
    redis-cli:macos)         echo "brew install redis && brew services start redis" ;;
    redis-cli:debian)        echo "sudo apt install -y redis-server && sudo systemctl enable --now redis-server" ;;
    redis-cli:fedora)        echo "sudo dnf install -y redis && sudo systemctl enable --now redis" ;;
    redis-cli:arch)          echo "sudo pacman -S --needed redis && sudo systemctl enable --now redis" ;;
    redis-cli:alpine)        echo "sudo apk add redis && rc-service redis start" ;;
    psql:macos)              echo "brew install postgresql@16 && brew services start postgresql@16" ;;
    psql:debian)             echo "sudo apt install -y postgresql postgresql-client" ;;
    psql:fedora)             echo "sudo dnf install -y postgresql-server postgresql && sudo postgresql-setup --initdb && sudo systemctl enable --now postgresql" ;;
    psql:arch)               echo "sudo pacman -S --needed postgresql" ;;
    psql:alpine)             echo "sudo apk add postgresql postgresql-client" ;;
    tectonic:macos)          echo "brew install tectonic" ;;
    tectonic:debian)         echo "sudo apt install -y tectonic  # (or: cargo install tectonic)" ;;
    tectonic:fedora)         echo "sudo dnf install -y tectonic" ;;
    tectonic:arch)           echo "sudo pacman -S --needed tectonic" ;;
    tectonic:*)              echo "cargo install tectonic" ;;
    pandoc:macos)            echo "brew install pandoc" ;;
    pandoc:debian)           echo "sudo apt install -y pandoc" ;;
    pandoc:fedora)           echo "sudo dnf install -y pandoc" ;;
    pandoc:arch)             echo "sudo pacman -S --needed pandoc" ;;
    pandoc:alpine)           echo "sudo apk add pandoc" ;;
    open-websearch:*)        echo "npm i -g open-websearch" ;;
    *)                       echo "see docs/install/${PLATFORM}.md" ;;
  esac
}

check() {
  # check <bin> <min-version> <required|optional> <version-cmd>
  local bin="$1" min="$2" tier="$3" vercmd="$4"
  if ! command -v "$bin" >/dev/null 2>&1; then
    if [ "$tier" = "required" ]; then
      printf '[ MISS ] %-15s not found        — install: %s\n' "$bin" "$(install_hint "$bin" "$tier")"
      FAIL=$((FAIL + 1))
    else
      printf '[ MISS ] %-15s not found (optional) — install: %s\n' "$bin" "$(install_hint "$bin" "$tier")"
      WARN=$((WARN + 1))
    fi
    return
  fi
  local found
  found="$(eval "$vercmd" 2>&1 | head -1 | grep -oE '[0-9]+(\.[0-9]+){1,2}' | head -1)"
  if [ -z "$found" ]; then
    printf '[ ?    ] %-15s installed (version unknown)\n' "$bin"
    return
  fi
  if ver_ge "$found" "$min"; then
    printf '[ OK   ] %-15s %s\n' "$bin" "$found"
  else
    if [ "$tier" = "required" ]; then
      printf '[ STALE] %-15s %s < %s   — upgrade: %s\n' "$bin" "$found" "$min" "$(install_hint "$bin" "$tier")"
      FAIL=$((FAIL + 1))
    else
      printf '[ STALE] %-15s %s < %s (optional) — upgrade: %s\n' "$bin" "$found" "$min" "$(install_hint "$bin" "$tier")"
      WARN=$((WARN + 1))
    fi
  fi
}

echo "Mathodology preflight ($PLATFORM)"
echo "------------------------------------------------------------"

# Required.
# Python 3.11 specifically — the worker pins target-python-version=3.11
# and uv's bytecode cache is keyed on minor version.
if command -v python3.11 >/dev/null 2>&1; then
  check python3.11 3.11.0 required "python3.11 --version"
elif command -v python3 >/dev/null 2>&1; then
  check python3    3.11.0 required "python3 --version"
else
  printf '[ MISS ] %-15s not found        — install: %s\n' "python3.11" "$(install_hint python3.11 required)"
  FAIL=$((FAIL + 1))
fi

check uv         0.4.0   required "uv --version"
check redis-cli  6.0.0   required "redis-cli --version"
check psql       14.0    required "psql --version"
check pandoc     2.0     required "pandoc --version"

# Optional.
check tectonic   0.14    optional "tectonic --version"
check node       20.0    optional "node --version"
check pnpm       9.0     optional "pnpm --version"
check open-websearch 0.0 optional "open-websearch --version"

echo "------------------------------------------------------------"
if [ "$FAIL" -gt 0 ]; then
  echo "FAIL: $FAIL required tool(s) missing or stale. See hints above."
  echo "Tip: scripts/install.sh installs everything for your platform."
  exit 1
fi
if [ "$WARN" -gt 0 ]; then
  echo "OK with $WARN optional tool(s) missing (some features may degrade)."
else
  echo "OK — all checks passed."
fi
exit 0
