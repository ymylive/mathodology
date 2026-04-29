# Install on Linux

Tested on Debian 12 / Ubuntu 22.04 LTS / Fedora 40 / Arch / Alpine 3.19. Both x86_64 and aarch64 are first-class.

There are four install paths. Pick one:

| Path | Best for | Time |
|------|----------|------|
| **A. Portable archive** (`tar.gz` from Releases) | One-machine developer use, easy to move/delete | 5 min |
| **B. `.deb` package** | Debian / Ubuntu servers — declares Postgres/Redis as deps, hooks into systemd | 5 min |
| **C. Docker compose** | Production / multi-host / clean teardown | 5 min — see [server.md](server.md) |
| **D. Source build** | You're hacking on the code | 15 min |

---

## 1. Prerequisites (paths A + D only)

The `.deb` (B) and Docker (C) paths handle dependencies for you. For A and D, install runtime prereqs once:

```bash
# Auto-detects apt / dnf / pacman / zypper / apk and installs everything
./scripts/install.sh         # interactive (prompts before sudo)
./scripts/install.sh --yes   # for CI / scripted use
./scripts/preflight.sh       # verify everything resolved
```

Or manually, per distribution:

### Debian / Ubuntu

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv postgresql postgresql-client \
                    redis-server pandoc tectonic
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo systemctl enable --now postgresql redis-server
sudo -u postgres psql -c "CREATE ROLE mm SUPERUSER LOGIN PASSWORD 'mm';" 2>/dev/null || true
sudo -u postgres createdb -O mm mm 2>/dev/null || true
```

### Fedora / RHEL

```bash
sudo dnf install -y python3.11 postgresql-server postgresql redis pandoc tectonic
sudo postgresql-setup --initdb
sudo systemctl enable --now postgresql redis
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo -u postgres psql -c "CREATE ROLE mm SUPERUSER LOGIN PASSWORD 'mm';" || true
sudo -u postgres createdb -O mm mm || true
```

### Arch

```bash
sudo pacman -S --needed python redis postgresql pandoc tectonic
sudo -iu postgres initdb -D /var/lib/postgres/data
sudo systemctl enable --now postgresql redis
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo -u postgres psql -c "CREATE ROLE mm SUPERUSER LOGIN PASSWORD 'mm';" || true
sudo -u postgres createdb -O mm mm || true
```

### Alpine

```bash
sudo apk add python3 py3-pip postgresql postgresql-client redis pandoc
# tectonic: install via cargo (no apk package): cargo install tectonic
sudo rc-update add postgresql && sudo rc-service postgresql start
sudo rc-update add redis      && sudo rc-service redis start
curl -LsSf https://astral.sh/uv/install.sh | sh
```

> **Tectonic is optional.** Without it, PDF export is disabled but everything else works. Install via your package manager when available, or `cargo install tectonic`.

---

## 2. Path A — portable archive

```bash
ARCH=$(uname -m); [ "$ARCH" = "arm64" ] && ARCH=aarch64
[ "$ARCH" = "x86_64" ] && ARCH=x86_64
VERSION=v0.4.0

curl -fLO "https://github.com/ymylive/mathodology/releases/download/$VERSION/mathodology-${VERSION#v}-linux-$ARCH.tar.gz"
curl -fLO "https://github.com/ymylive/mathodology/releases/download/$VERSION/SHA256SUMS.txt"
sha256sum -c SHA256SUMS.txt --ignore-missing

tar -xzf mathodology-${VERSION#v}-linux-$ARCH.tar.gz
cd mathodology-${VERSION#v}-linux-$ARCH

cp .env.example .env       # edit; set at least one *_API_KEY
./run.sh
```

Open <http://127.0.0.1:8080/>.

---

## 3. Path B — `.deb` package (Debian / Ubuntu)

```bash
VERSION=v0.4.0
curl -fLO "https://github.com/ymylive/mathodology/releases/download/$VERSION/mathodology_${VERSION#v}_amd64.deb"
sudo apt install ./mathodology_${VERSION#v}_amd64.deb
```

`apt` resolves `postgresql-client`, `redis-tools`, `python3.11`, `pandoc` for you. Tectonic is recommended but optional.

After install:

```bash
# Edit /etc/mathodology/.env (template was copied from .env.example)
sudoedit /etc/mathodology/.env

# Bring up the service
sudo systemctl enable --now mathodology.target

# Health
sudo systemctl status mathodology-gateway mathodology-worker
journalctl -u mathodology-gateway -f
```

The `mathodology` user is created during postinstall; both processes drop privileges. Files live under `/usr/share/mathodology/`, runtime artifacts under `/var/lib/mathodology/runs/`.

Uninstall:

```bash
sudo apt remove mathodology          # keep config
sudo apt purge  mathodology          # remove config + user
```

---

## 4. Path C — Docker compose

Best for production. See [docs/install/server.md](server.md). Quick version:

```bash
git clone https://github.com/ymylive/mathodology.git && cd mathodology
cp .env.example .env       # edit; set at least one *_API_KEY
docker compose -f docker-compose.prod.yml up -d
# Web on http://localhost:8081, gateway on :8080.
```

---

## 5. Path D — source build

```bash
git clone https://github.com/ymylive/mathodology.git && cd mathodology
./scripts/install.sh --with-source   # adds node + pnpm
cp .env.example .env                 # edit
just bootstrap                       # cargo fetch + uv sync + pnpm install
just migrate                         # sqlx migrations
just dev                             # gateway + worker + vite dev :5173
```

Requires `just` (`cargo install just` or `apt install just`).

---

## 6. Troubleshooting

| Symptom | Fix |
|---------|-----|
| Port 8080 already in use | `GATEWAY_PORT=9090 ./run.sh` (or set in `.env`) |
| `peer authentication failed for user "mm"` | Edit `/etc/postgresql/*/main/pg_hba.conf`: change `peer` → `md5` for `local` lines, then `sudo systemctl reload postgresql` |
| `redis-cli` not found but Redis is running | `apt install redis-tools` (some distros split server/client) |
| systemd: `Failed to start: status=217/USER` | `sudo useradd -r -s /usr/sbin/nologin mathodology` then `sudo systemctl daemon-reload` |
| Worker exits with `ModuleNotFoundError: agent_worker` | `cd /usr/share/mathodology/worker && uv sync` (one-time first run) |
| `tectonic: command not found` | PDF export disabled. Install via cargo: `cargo install tectonic` |
| `glibc not found` (very old distros) | Use the Docker path; the .tar.gz needs glibc 2.31+ (Ubuntu 20.04 / Debian 11) |

For production hardening, monitoring, and HTTPS, see [server.md](server.md).
