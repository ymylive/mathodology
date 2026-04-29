#!/usr/bin/env bash
# Build the Mathodology macOS .pkg from a release archive layout.
#
# Inputs (from CWD, which should be the unpacked release archive):
#   ./gateway                       (binary, +x; arm64 or x86_64)
#   ./apps/web/dist/                (prebuilt SPA)
#   ./apps/agent-worker/            (Python source)
#   ./packages/py-contracts/        (Python contracts)
#   ./crates/gateway/migrations/    (sqlx migrations)
#   ./config/providers.toml
#   ./config/launchd/com.mathodology.gateway.plist
#   ./config/launchd/com.mathodology.worker.plist
#   ./.env.example
#
# Env:
#   VERSION   required, e.g. 0.3.0
#   OUTDIR    optional, default = CWD; final pkg = $OUTDIR/mathodology-$VERSION.pkg
#
# IMPORTANT: this .pkg is UNSIGNED. We have no Apple Developer ID. Users
# downloading from a browser will need:
#     xattr -d com.apple.quarantine ./mathodology-*.pkg
# ...or to allow it via System Settings -> Privacy & Security.
set -euo pipefail

# Strip macOS resource forks / AppleDouble (._*) files from the payload.
# pkgbuild preserves them by default which bloats the .pkg and triggers
# spurious diffs on restore.
export COPYFILE_DISABLE=1

: "${VERSION:?VERSION env var required (e.g. VERSION=0.3.0 $0)}"
OUTDIR="${OUTDIR:-$PWD}"
HERE="$(cd "$(dirname "$0")" && pwd)"

PKGROOT="$(mktemp -d -t mm-pkg-root.XXXXXX)"
trap 'rm -rf "$PKGROOT"' EXIT

PREFIX="$PKGROOT/usr/local/mathodology"
DAEMONS="$PKGROOT/Library/LaunchDaemons"

mkdir -p "$PREFIX" "$DAEMONS"

# ---------- 1. stage payload ----------
echo "==> staging payload at $PREFIX"

# Gateway binary.
[ -x ./gateway ] || { echo "!! ./gateway missing or not +x" >&2; exit 1; }
install -m 0755 ./gateway "$PREFIX/gateway"

# Mirror release tree. Use rsync --include patterns so missing dirs don't
# silently produce an empty pkg.
copy_dir() {
    src="$1"; dst="$2"
    if [ ! -d "$src" ]; then
        echo "!! $src missing" >&2; exit 1
    fi
    mkdir -p "$dst"
    # macOS ships openrsync which lacks --no-xattrs / --no-perms; we strip
    # AppleDouble files in a post-pass below instead.
    rsync -a \
          --exclude='.DS_Store' --exclude='__pycache__' --exclude='*.pyc' \
          --exclude='.venv' --exclude='node_modules' --exclude='._*' \
          "$src/" "$dst/"
}

copy_dir ./apps/web/dist                  "$PREFIX/apps/web/dist"
copy_dir ./apps/agent-worker              "$PREFIX/apps/agent-worker"
copy_dir ./packages/py-contracts          "$PREFIX/packages/py-contracts"
copy_dir ./crates/gateway/migrations      "$PREFIX/migrations"

install -m 0644 ./config/providers.toml   "$PREFIX/providers.toml"
install -m 0644 ./.env.example            "$PREFIX/.env.example"

# Carry release-run.sh + preflight.sh so the operator can also start it
# manually outside launchd.
if [ -f ./scripts/release-run.sh ]; then
    install -m 0755 ./scripts/release-run.sh "$PREFIX/release-run.sh"
fi
if [ -f ./scripts/preflight.sh ]; then
    mkdir -p "$PREFIX/scripts"
    install -m 0755 ./scripts/preflight.sh "$PREFIX/scripts/preflight.sh"
fi

# ---------- 2. launchd plists ----------
echo "==> staging LaunchDaemons"
install -m 0644 ./config/launchd/com.mathodology.gateway.plist "$DAEMONS/"
install -m 0644 ./config/launchd/com.mathodology.worker.plist  "$DAEMONS/"

# Strip any AppleDouble (._*) files that crept in despite COPYFILE_DISABLE
# (e.g. files copied from an HFS+ volume earlier in the pipeline).
find "$PKGROOT" -name '._*' -delete 2>/dev/null || true

# ---------- 3. component pkg ----------
COMPONENT_PKG="$(mktemp -t mm-component.XXXXXX).pkg"
trap 'rm -rf "$PKGROOT" "$COMPONENT_PKG"' EXIT

echo "==> pkgbuild -> $COMPONENT_PKG"
pkgbuild \
    --root "$PKGROOT" \
    --identifier "com.mathodology.pkg" \
    --version "$VERSION" \
    --scripts "$HERE/scripts" \
    --install-location "/" \
    "$COMPONENT_PKG"

# ---------- 4. distribution pkg (productbuild) ----------
# Render a temporary distribution.xml with VERSION substituted in.
DIST_TMP="$(mktemp -t mm-dist.XXXXXX).xml"
trap 'rm -rf "$PKGROOT" "$COMPONENT_PKG" "$DIST_TMP"' EXIT
sed "s|@@VERSION@@|$VERSION|g; s|@@COMPONENT_PKG@@|$(basename "$COMPONENT_PKG")|g" \
    "$HERE/distribution.xml" > "$DIST_TMP"

OUT_PKG="$OUTDIR/mathodology-$VERSION.pkg"
echo "==> productbuild -> $OUT_PKG"
productbuild \
    --distribution "$DIST_TMP" \
    --resources "$HERE" \
    --package-path "$(dirname "$COMPONENT_PKG")" \
    "$OUT_PKG"

echo "==> done: $OUT_PKG"
echo "    (unsigned — users may need: xattr -d com.apple.quarantine '$OUT_PKG')"
