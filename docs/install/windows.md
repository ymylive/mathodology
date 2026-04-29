# Install on Windows

Tested on Windows 11 (x86_64) and Windows Server 2022. PowerShell 5.1 minimum; PowerShell 7 recommended.

There are three install paths:

| Path | Best for | Time |
|------|----------|------|
| **A. Portable archive** (`zip` from Releases) | Single-user, run from a folder | 10 min |
| **B. `.msi` installer** | Multi-user, Start Menu integration, optional Windows Service | 10 min |
| **C. Source build** | Developers | 30 min |

---

## 1. Prerequisites

Run **PowerShell as Administrator**, then:

```powershell
# Auto-installer — uses winget where available, falls back to scoop
.\scripts\install.ps1            # interactive
.\scripts\install.ps1 -Yes       # CI / scripted
.\scripts\preflight.ps1          # verify
```

Or manually:

```powershell
# Mandatory
winget install --id Python.Python.3.11           -e --accept-package-agreements --accept-source-agreements
winget install --id PostgreSQL.PostgreSQL        -e
winget install --id Memurai.MemuraiDeveloper     -e   # Redis-compatible server for Windows
winget install --id JohnMacFarlane.Pandoc        -e
winget install --id astral-sh.uv                 -e

# Optional (PDF export)
winget install --id Tectonic.Tectonic            -e

# Optional (source builds)
winget install --id OpenJS.NodeJS.LTS            -e
corepack enable
corepack prepare pnpm@latest --activate

# Refresh PATH in the current session
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
```

### Postgres setup

The PostgreSQL installer prompts for a `postgres` superuser password. Remember it. Then:

```powershell
$env:PGPASSWORD = '<that password>'
& 'C:\Program Files\PostgreSQL\16\bin\psql.exe' -U postgres -c "CREATE ROLE mm SUPERUSER LOGIN PASSWORD 'mm';"
& 'C:\Program Files\PostgreSQL\16\bin\createdb.exe' -U postgres -O mm mm
```

Add `C:\Program Files\PostgreSQL\16\bin` to your PATH (System Properties → Environment Variables) so `psql`, `pg_isready` work directly.

### Memurai (Redis on Windows)

Memurai is a free Redis-compatible server. After install it's a Windows service named `Memurai`:

```powershell
Start-Service Memurai
Get-Service Memurai     # should be Running
```

Native Redis isn't supported on Windows. If you'd rather not use Memurai, run Redis under WSL2 or in Docker — both work fine, just point `REDIS_URL` at the right host.

---

## 2. Path A — portable archive

```powershell
$VERSION = "v0.4.0"
$ARCH    = "windows-x86_64"
Invoke-WebRequest "https://github.com/ymylive/mathodology/releases/download/$VERSION/mathodology-$($VERSION.TrimStart('v'))-$ARCH.zip" -OutFile "mm.zip"
Invoke-WebRequest "https://github.com/ymylive/mathodology/releases/download/$VERSION/SHA256SUMS.txt" -OutFile "SHA256SUMS.txt"

# Verify
(Get-FileHash mm.zip -Algorithm SHA256).Hash
Get-Content SHA256SUMS.txt | Select-String "windows-x86_64"

Expand-Archive mm.zip -DestinationPath .
cd mathodology-$($VERSION.TrimStart('v'))-$ARCH

Copy-Item .env.example .env
notepad .env                 # add at least one *_API_KEY
.\run.ps1
```

Open <http://127.0.0.1:8080/>.

`run.ps1` loads `.env`, calls `preflight.ps1`, runs `uv sync` for the worker, and starts gateway + worker as background jobs. `Ctrl-C` stops both.

### SmartScreen warning

`gateway.exe` is unsigned. Windows SmartScreen will show "Windows protected your PC" on first run. Click **More info** → **Run anyway**, or unblock the file:

```powershell
Unblock-File .\gateway.exe
```

---

## 3. Path B — `.msi` installer

```powershell
$VERSION = "v0.4.0"
Invoke-WebRequest "https://github.com/ymylive/mathodology/releases/download/$VERSION/mathodology-$($VERSION.TrimStart('v')).msi" -OutFile "mm.msi"
Start-Process msiexec -ArgumentList "/i mm.msi /qb" -Wait
```

Default install dir: `C:\Program Files\Mathodology\`. The installer:

- Drops `gateway.exe`, `apps\web\dist`, `apps\agent-worker`, scripts, configs
- Adds the install dir to system PATH
- Creates Start Menu shortcuts: **Mathodology (Run)** and **Open UI**
- Copies `.env.example` → `.env` if `.env` doesn't already exist

After install, edit the env file:

```powershell
notepad "C:\Program Files\Mathodology\.env"
```

Then either run interactively (Start Menu → "Mathodology (Run)") or install as Windows Services:

```powershell
# Requires NSSM. Install once: winget install NSSM.NSSM
cd "C:\Program Files\Mathodology\config\windows"
.\install-service.ps1
```

This creates two services: `Mathodology-Gateway` and `Mathodology-Worker`, both `StartType=Automatic`. Logs go to `C:\ProgramData\Mathodology\logs\`.

Uninstall:

```powershell
.\uninstall-service.ps1                                        # if you registered services
Start-Process msiexec -ArgumentList "/x mm.msi /qb" -Wait      # remove the install
```

The `.msi` is **unsigned**. SmartScreen will warn the first time.

---

## 4. Path C — source build

Requires Visual Studio Build Tools (for some Rust crates that link to native code) and Git.

```powershell
winget install --id Microsoft.VisualStudio.2022.BuildTools --override "--quiet --add Microsoft.VisualStudio.Workload.VCTools"
winget install --id Git.Git
winget install --id Rustlang.Rustup
rustup toolchain install 1.83
rustup default 1.83

git clone https://github.com/ymylive/mathodology.git
cd mathodology

.\scripts\install.ps1 -WithSource    # adds Node + pnpm
Copy-Item .env.example .env
notepad .env

# Bootstrap (mirrors `just bootstrap`)
cargo fetch
uv sync
pnpm install

# Apply migrations
cd crates\gateway; sqlx migrate run; cd ..\..

# Run the three processes — easiest is three separate terminals
cargo run -p gateway              # terminal 1
cd apps\agent-worker; uv run python -m agent_worker   # terminal 2
pnpm --filter web dev             # terminal 3 — opens http://localhost:5173
```

(`just` works on Windows too: `winget install Casey.Just` then `just dev` — but it relies on bash, so install Git Bash or use WSL for the best experience.)

---

## 5. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `psql` / `pg_isready` not found | Add `C:\Program Files\PostgreSQL\16\bin` to PATH and reopen the shell |
| `redis-cli` not found | Memurai installs `memurai-cli.exe` instead — alias it: `Set-Alias redis-cli memurai-cli` |
| Port 8080 in use | Set `GATEWAY_PORT=9090` in `.env` and re-run |
| `.\run.ps1` blocked by execution policy | `Set-ExecutionPolicy -Scope Process Bypass` (one shell only), or `powershell -ExecutionPolicy Bypass -File .\run.ps1` |
| `gateway.exe` blocked by SmartScreen | `Unblock-File .\gateway.exe` |
| Worker prints `ModuleNotFoundError` | `cd apps\agent-worker; uv sync` (one-time) |
| Postgres `password authentication failed for user "mm"` | Recreate role: `psql -U postgres -c "ALTER USER mm WITH PASSWORD 'mm';"` |
| WSL preferred? | Install Linux on WSL2 and follow [docs/install/linux.md](linux.md) instead |

For headless / multi-user / production, see [server.md](server.md).
