# One-shot bootstrap for a Mathodology release archive (Windows PowerShell).
#
# Same contract as scripts/release-run.sh. Requires: uv, Redis reachable,
# Postgres reachable, sqlx-cli (optional — otherwise migrations skipped
# and the gateway will apply them on startup).

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

# Load .env if present, else copy from .env.example.
if (Test-Path .env) {
    Get-Content .env | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            [System.Environment]::SetEnvironmentVariable($Matches[1], $Matches[2])
        }
    }
} elseif (Test-Path .env.example) {
    Write-Host "!! .env missing; copying from .env.example (edit to add API keys)"
    Copy-Item .env.example .env
    & $PSCommandPath
    exit
}

if (-not $env:REDIS_URL)    { $env:REDIS_URL    = "redis://127.0.0.1:6379/0" }
if (-not $env:DATABASE_URL) { $env:DATABASE_URL = "postgres://mm:mm@127.0.0.1:5432/mm" }
if (-not $env:GATEWAY_HOST) { $env:GATEWAY_HOST = "127.0.0.1" }
if (-not $env:GATEWAY_PORT) { $env:GATEWAY_PORT = "8080" }
if (-not $env:RUNS_DIR)     { $env:RUNS_DIR     = Join-Path $PSScriptRoot "runs" }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv not found (install from https://github.com/astral-sh/uv)"
}

Write-Host "==> syncing Python worker deps"
Push-Location apps\agent-worker
try { uv sync --frozen } catch { uv sync }
Pop-Location

if (Get-Command sqlx -ErrorAction SilentlyContinue) {
    Write-Host "==> applying sqlx migrations"
    Push-Location crates\gateway
    sqlx migrate run --database-url $env:DATABASE_URL
    Pop-Location
}

New-Item -ItemType Directory -Force -Path $env:RUNS_DIR | Out-Null

Write-Host "==> starting gateway on $env:GATEWAY_HOST`:$env:GATEWAY_PORT"
$gateway = Start-Process -FilePath .\gateway.exe -PassThru -NoNewWindow

Write-Host "==> starting worker"
Push-Location apps\agent-worker
$worker = Start-Process -FilePath uv -ArgumentList "run","python","-m","agent_worker" -PassThru -NoNewWindow
Pop-Location

Write-Host ""
Write-Host "Mathodology stack is up."
Write-Host "  Gateway  http://$($env:GATEWAY_HOST):$($env:GATEWAY_PORT)"
Write-Host "  UI dist  apps\web\dist  (serve with any static host)"
Write-Host ""
Write-Host "Ctrl-C to stop."

try {
    Wait-Process -Id $gateway.Id, $worker.Id
} finally {
    Stop-Process -Id $gateway.Id -ErrorAction SilentlyContinue
    Stop-Process -Id $worker.Id -ErrorAction SilentlyContinue
}
