# Install on macOS

Tested on macOS 13 Ventura and later, both Intel and Apple Silicon.

There are three install paths. Pick one:

| Path | Best for | Time |
|------|----------|------|
| **A. Portable archive** (`tar.gz` from Releases) | You want a single folder you can move/delete | 5 min |
| **B. `.pkg` installer** | You want it under `/usr/local/mathodology` with launchd auto-start | 5 min |
| **C. Source build** | You're hacking on the code | 15 min |

All three need the same prerequisites — install them once.

---

## 1. Prerequisites

```bash
# Homebrew (skip if you already have it)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Everything Mathodology needs
brew install python@3.11 redis postgresql@16 pandoc tectonic node
brew services start redis
brew services start postgresql@16

# uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Postgres role + database
createuser -s mm 2>/dev/null || true
createdb -O mm mm 2>/dev/null || true
```

Or, if you've already unpacked the archive (path A) or cloned the repo (path C):

```bash
./scripts/install.sh        # interactive — confirms before installing
./scripts/install.sh --yes  # CI / scripted
./scripts/preflight.sh      # verify everything is present
```

`install.sh` is **idempotent**: it skips anything already installed, asks before running `brew install`, and prints a final summary.

---

## 2. Path A — portable archive (recommended)

```bash
# Download for your CPU. uname -m → arm64 (Apple Silicon) or x86_64 (Intel).
ARCH=$(uname -m); [ "$ARCH" = "arm64" ] && ARCH=aarch64
VERSION=v0.4.0
curl -fLO "https://github.com/ymylive/mathodology/releases/download/$VERSION/mathodology-${VERSION#v}-macos-$ARCH.tar.gz"
curl -fLO "https://github.com/ymylive/mathodology/releases/download/$VERSION/SHA256SUMS.txt"
shasum -a 256 -c SHA256SUMS.txt --ignore-missing

tar -xzf mathodology-${VERSION#v}-macos-$ARCH.tar.gz
cd mathodology-${VERSION#v}-macos-$ARCH

cp .env.example .env       # then edit .env to add at least one *_API_KEY
./run.sh
```

Open <http://127.0.0.1:8080/> — the gateway hosts both the API and the SPA.

`./run.sh` is the same script as in Linux: it loads `.env`, runs `preflight.sh`, syncs the worker venv, and starts gateway + worker. `Ctrl-C` stops both.

### macOS quarantine (Gatekeeper)

If macOS refuses to run the binary because it's "from an unidentified developer", clear the quarantine bit:

```bash
xattr -d com.apple.quarantine ./gateway
```

Or right-click the binary in Finder → Open → Open Anyway.

---

## 3. Path B — `.pkg` installer

```bash
VERSION=v0.4.0
curl -fLO "https://github.com/ymylive/mathodology/releases/download/$VERSION/mathodology-${VERSION#v}.pkg"
xattr -d com.apple.quarantine ./mathodology-${VERSION#v}.pkg
sudo installer -pkg ./mathodology-${VERSION#v}.pkg -target /
```

This installs to `/usr/local/mathodology/` and registers two launchd daemons:

- `com.mathodology.gateway` — the Rust gateway
- `com.mathodology.worker`  — the Python worker

Configure and start:

```bash
sudo $EDITOR /usr/local/mathodology/.env       # add API keys, change DEV_AUTH_TOKEN
sudo launchctl kickstart -k system/com.mathodology.gateway
sudo launchctl kickstart -k system/com.mathodology.worker
```

Logs:

```bash
tail -f /usr/local/mathodology/logs/gateway.log
tail -f /usr/local/mathodology/logs/worker.log
```

Uninstall:

```bash
sudo launchctl bootout system /Library/LaunchDaemons/com.mathodology.gateway.plist
sudo launchctl bootout system /Library/LaunchDaemons/com.mathodology.worker.plist
sudo rm -rf /usr/local/mathodology /Library/LaunchDaemons/com.mathodology.*.plist
```

The `.pkg` is **unsigned** (no Apple Developer ID). Gatekeeper will warn — `xattr -d com.apple.quarantine` clears it.

---

## 4. Path C — source build (developers)

```bash
git clone https://github.com/ymylive/mathodology.git
cd mathodology
cp .env.example .env       # edit
./scripts/install.sh --with-source   # adds node + pnpm on top of runtime tools
just bootstrap             # cargo fetch + uv sync + pnpm install
just migrate               # apply sqlx migrations
just dev                   # gateway :8080 + worker + vite dev :5173
```

Open <http://localhost:5173/>. (Vite dev server proxies API calls to the gateway on `:8080`.)

---

## 5. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `redis-cli ping` → connection refused | `brew services start redis` |
| `pg_isready` → no response | `brew services restart postgresql@16` |
| `gateway: Permission denied` | `chmod +x ./gateway` (lost during unzip) |
| Browser shows "blank page, 404 on /assets/*" | `STATIC_DIR` is empty; set it to `./apps/web/dist` (path A's `.env` does this for you) |
| `tectonic: command not found` | PDF export disabled; install tectonic (`brew install tectonic`) and restart |
| pnpm complains about node version | `nvm install 20` or `brew upgrade node` |
| Apple Silicon: `bad CPU type in executable` | You downloaded the x86_64 build — re-download the `aarch64` one |

For deeper issues see [docs/install/server.md](server.md) (production deployment) or open an issue on GitHub.
