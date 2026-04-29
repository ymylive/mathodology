# Production deployment

This guide is for running Mathodology as a long-lived service — single host or a small cluster. For one-developer-laptop install, see [linux.md](linux.md) / [macos.md](macos.md) / [windows.md](windows.md).

We recommend Docker Compose for new deployments. systemd is documented as the bare-metal alternative.

---

## 1. Recommended: Docker Compose

The repo ships a complete `docker-compose.prod.yml` covering five services:

```
postgres       postgres:16-alpine    persistent volume
redis          redis:7-alpine        AOF persistence
gateway        Dockerfile.gateway    your binary, port 8080
worker         Dockerfile.worker     uv + Python 3.11 + tectonic + pandoc
web            caddy:2-alpine        serves SPA + reverse-proxies API → gateway
```

Caddy fronts both the SPA and the API on port 8081, so end users hit a single origin and the gateway never has to be exposed publicly.

### 1.1 First-time setup

```bash
git clone https://github.com/ymylive/mathodology.git
cd mathodology

cp .env.example .env
# Edit .env. At a minimum:
#   DEV_AUTH_TOKEN=...                # CHANGE from default
#   DEEPSEEK_API_KEY=... (or OPENAI_API_KEY=... or ANTHROPIC_API_KEY=...)
$EDITOR .env

# Web bundle must be built once (host-side) and bind-mounted into the caddy container.
# Easiest: pull the corresponding release archive and copy its dist/, OR build from source.
pnpm install --frozen-lockfile
pnpm --filter web build           # produces apps/web/dist

# Bring it up
docker compose -f docker-compose.prod.yml up -d --build

# Health
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f gateway worker
```

UI: <http://YOUR-HOST:8081/>. Gateway directly: <http://YOUR-HOST:8080/health> (loopback-bound by default; expose only if you trust the network).

### 1.2 Pre-built images from GHCR

If you don't want to build locally:

```yaml
# docker-compose.prod.yml — replace the `build:` blocks with:
gateway:
  image: ghcr.io/ymylive/mathodology-gateway:v0.4.0
worker:
  image: ghcr.io/ymylive/mathodology-worker:v0.4.0
```

Multi-arch images (`linux/amd64` + `linux/arm64`) are pushed by the release workflow on every tag.

### 1.3 Updating

```bash
git pull                                                      # or just edit the image tag
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

The gateway runs sqlx migrations at startup, so DB schema changes are applied automatically. **Migrations are forward-only** — there's no downgrade path. Take a Postgres snapshot before major-version upgrades.

### 1.4 Backups

Two volumes hold state:

```bash
# Postgres logical dump
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U mm -d mm | gzip > backups/mm-$(date +%F).sql.gz

# Run artifacts (figures, notebooks, papers)
tar -C runs -czf backups/runs-$(date +%F).tar.gz .

# Redis is an event bus, not state — no backup needed.
```

Restore:

```bash
gunzip -c backups/mm-2026-04-29.sql.gz | docker compose exec -T postgres psql -U mm -d mm
```

### 1.5 HTTPS in front

Drop a TLS terminator (Caddy on the host, Nginx, Cloudflare Tunnel, …) in front of the `web` service. The bundled Caddy listens on plain HTTP only — it's an internal-network service.

Example: extend the bundled Caddyfile with auto-HTTPS by editing `config/Caddyfile.prod`:

```caddyfile
mathodology.example.com {     # replace :80 with your domain
    encode zstd gzip
    # ... existing rules ...
}
```

…and expose port 443 in compose. Caddy auto-issues from Let's Encrypt.

---

## 2. Bare-metal: systemd (Linux)

If you can't run Docker, use the `.deb` (Debian/Ubuntu — see [linux.md](linux.md#path-b)) or assemble it manually.

The repo ships unit files at `config/systemd/`:

```
mathodology.target              groups gateway + worker
mathodology-gateway.service     ExecStart=/opt/mathodology/gateway
mathodology-worker.service      ExecStart=uv run python -m agent_worker
install.sh                      idempotent installer
```

```bash
# Lay down the release archive at /opt/mathodology
sudo tar -C /opt -xzf mathodology-0.4.0-linux-x86_64.tar.gz
sudo mv /opt/mathodology-0.4.0-linux-x86_64 /opt/mathodology

# Install the units (creates the mathodology user, copies units, daemon-reload, enable+start)
sudo bash /opt/mathodology/config/systemd/install.sh

# Edit env BEFORE first start (or units will use defaults)
sudo $EDITOR /opt/mathodology/.env
sudo systemctl restart mathodology.target

# Watch
journalctl -u mathodology-gateway -f
journalctl -u mathodology-worker -f
```

The units use `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`. Only `/opt/mathodology/runs` is writable.

Postgres + Redis still need to be installed and running (see [linux.md §1](linux.md#1-prerequisites)). The units `Wants=postgresql.service redis-server.service`, so they will start after them but won't fail to launch if those names differ in your distro — adjust the `After=` line in the units if your service names differ.

---

## 3. Production hardening checklist

| | |
|---|---|
| **DEV_AUTH_TOKEN** | Replace `dev-local-insecure-token` with a long random string. The gateway requires this on every authed request. |
| **TLS** | Always terminate TLS in front of the web tier. Caddy auto-issues; Nginx + certbot is the classic alternative. |
| **Postgres password** | Default is `mm` / `mm` — change `POSTGRES_PASSWORD` in `.env` and recreate the DB, or `ALTER USER mm WITH PASSWORD '...';`. |
| **Postgres backups** | At least daily `pg_dump` to off-host storage (S3, B2, …). |
| **Runs disk** | Each run writes 5–50 MB of figures + notebooks. Mount `/data/runs` on a sized volume; add a cleanup cron to delete `runs/<run_id>/` older than N days if needed. |
| **LLM cost ceiling** | The cost ledger tracks per-run spend in Postgres. There's no built-in hard cap — wrap your client to refuse new runs above your budget, or query `SELECT SUM(cost_rmb) FROM runs WHERE created_at >= now() - interval '24 hours'` from a cron. |
| **Logs** | Both services log to stdout. Compose captures via `docker logs`; systemd via `journalctl`. Forward to your aggregator (Loki, ELK, Datadog) via the standard Docker logging driver / journald drop-in. |
| **Resource limits** | The worker spawns Jupyter kernels that can spike memory on heavy SciPy work. Set `mem_limit: 4g` per worker container or systemd's `MemoryMax=4G`. |
| **Open ports** | Default compose binds gateway to `127.0.0.1:8080` and web (caddy) to `0.0.0.0:8081`. The gateway is meant to be internal — only expose web externally. |

---

## 4. Multi-tenant / scale-out

**Today: single-tenant.** Mathodology has no JWT auth, no per-user isolation, no quota system. Treat it as a service for one team.

For team use behind SSO, the cleanest path today:

- Front Caddy with an auth proxy (oauth2-proxy, Pomerium, Authelia) gating `/`.
- Make `DEV_AUTH_TOKEN` a per-user value injected by the proxy as `Authorization: Bearer ...` (every request the SPA makes already carries it).

Multi-instance (horizontal worker scale) works as-is: Redis Streams plus consumer groups (`mm-workers`) round-robin job dispatch. Add `--scale worker=4` to compose and you have 4 worker containers competing for jobs. The gateway scales similarly behind a load balancer; sessions/state live in Postgres + Redis only.

DB and Redis are single instances in the bundled compose. For HA: use managed Postgres + Redis (RDS, ElastiCache, Aiven, etc.) and point `DATABASE_URL` / `REDIS_URL` at them.

---

## 5. Observability quickstart

Out of the box: structured logs (JSON when `RUST_LOG` is set, pretty otherwise) on stdout. No metrics endpoint yet.

For a minimal observability stack:

```bash
# Add a Prometheus + Grafana sidecar — both scrape nothing today, but you can
# use cAdvisor for container metrics and node-exporter for host metrics.
docker compose -f docker-compose.prod.yml \
              -f docker-compose.observability.yml up -d
```

(The observability compose file is **not** shipped — write one matching your stack. Roadmap: a Prometheus exporter on the gateway in v0.5+.)

---

## 6. Common production issues

| Symptom | Diagnosis |
|---------|-----------|
| Gateway fails to start with `pool timed out` | Postgres unreachable. `docker compose ps postgres` → expect `(healthy)`. Check `docker compose logs postgres`. |
| Worker idle, gateway accepts runs but nothing progresses | Worker can't reach gateway. Check `GATEWAY_HTTP` env in worker. In compose it must be `http://gateway:8080`, not `http://localhost:8080`. |
| `kernel.figure` events missing | The Coder agent uses `display(fig)` (which produces inline images) **and** `plt.savefig()` (which only writes to disk). The on-disk fallback is what's served by the gateway over `/runs/:id/figures/*`. Both paths exist. |
| 100 % CPU on worker for minutes | Normal for matplotlib-heavy scripts; SciPy optimizers are CPU-bound. Set `mem_limit` if Python OOMs. |
| Disk fills up under `runs/` | Add a daily cron: `find runs -maxdepth 1 -mindepth 1 -type d -mtime +14 -exec rm -rf {} +` |
| Cost runaway | Inspect `runs.cost_rmb` in Postgres; correlate with `events_audit` to find which agent/model spent the budget. |

---

For development workflow (writing tests, running CI, contracts codegen), see the main [README.md](../../README.md) and [CONTRIBUTING.md](../../CONTRIBUTING.md).
