# One-shot launcher for a Mathodology release archive (Windows PowerShell 5.1+).
#
# Same contract as scripts/release-run.sh. Differences from the previous
# version (fixes):
#   - .env parser strips quotes, ignores comments / blank lines.
#   - No self-recursion; missing .env => copy + exit so user can fill keys.
#   - Process management uses Wait-Job, which behaves correctly with
#     -NoNewWindow children unlike Wait-Process+PassThru.
#   - STATIC_DIR defaults to .\apps\web\dist so the gateway hosts the UI.
#   - Calls scripts\preflight.ps1 if present.

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $PSCommandPath
Set-Location $ROOT

# ---------- 1. .env loading ----------
function Load-DotEnv($path) {
    Get-Content -LiteralPath $path | ForEach-Object {
        $line = $_
        if ($line -match '^\s*#') { return }
        if ($line -match '^\s*$') { return }
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $k = $Matches[1]
            $v = $Matches[2]
            # Strip matching surrounding quotes (single or double).
            if ($v.Length -ge 2 -and (
                  ($v[0] -eq '"' -and $v[-1] -eq '"') -or
                  ($v[0] -eq "'" -and $v[-1] -eq "'")
                )) {
                $v = $v.Substring(1, $v.Length - 2)
            }
            [System.Environment]::SetEnvironmentVariable($k, $v, "Process")
        }
    }
}

if (-not (Test-Path .env)) {
    if (Test-Path .env.example) {
        Write-Host "!! .env missing — copying from .env.example. Edit it then re-run."
        Copy-Item .env.example .env
        Write-Host "   At minimum, set one of DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY."
        exit 1
    } else {
        Write-Error "!! .env.example also missing — corrupt archive?"
    }
}
Load-DotEnv (Join-Path $ROOT ".env")

# ---------- 2. defaults ----------
function Default-Env([string]$key, [string]$value) {
    if (-not [System.Environment]::GetEnvironmentVariable($key, "Process")) {
        [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
}
Default-Env "REDIS_URL"        "redis://127.0.0.1:6379/0"
Default-Env "DATABASE_URL"     "postgres://mm:mm@127.0.0.1:5432/mm"
Default-Env "GATEWAY_HOST"     "127.0.0.1"
Default-Env "GATEWAY_PORT"     "8080"
Default-Env "RUNS_DIR"         (Join-Path $ROOT "runs")
Default-Env "STATIC_DIR"       (Join-Path $ROOT "apps\web\dist")
Default-Env "DEV_AUTH_TOKEN"   "dev-local-insecure-token"
Default-Env "GATEWAY_HTTP"     "http://$($env:GATEWAY_HOST):$($env:GATEWAY_PORT)"

New-Item -ItemType Directory -Force -Path $env:RUNS_DIR | Out-Null

# ---------- 3. preflight ----------
$preflight = Join-Path $ROOT "scripts\preflight.ps1"
if (Test-Path $preflight) {
    & $preflight
    if ($LASTEXITCODE -ne 0) {
        Write-Host "!! preflight reported missing prerequisites (see above)."
        Write-Host "   Run scripts\install.ps1 to install them automatically, or fix manually."
        exit 1
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "!! uv not found. Install: irm https://astral.sh/uv/install.ps1 | iex"
}

# ---------- 4. worker venv ----------
Write-Host "==> uv sync (worker)"
Push-Location apps\agent-worker
try {
    & uv sync --frozen 2>$null
    if ($LASTEXITCODE -ne 0) { & uv sync }
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
} finally {
    Pop-Location
}

# ---------- 5. start ----------
$gatewayBin = Join-Path $ROOT "gateway.exe"
if (-not (Test-Path $gatewayBin)) {
    Write-Error "!! gateway.exe not found at $gatewayBin"
}

Write-Host "==> starting gateway on http://$($env:GATEWAY_HOST):$($env:GATEWAY_PORT)"
$gatewayJob = Start-Job -ScriptBlock {
    param($bin, $envVars)
    foreach ($k in $envVars.Keys) {
        [System.Environment]::SetEnvironmentVariable($k, $envVars[$k], "Process")
    }
    & $bin
} -ArgumentList $gatewayBin, ([Environment]::GetEnvironmentVariables("Process"))

Write-Host "==> starting worker"
$workerJob = Start-Job -ScriptBlock {
    param($wd, $envVars)
    foreach ($k in $envVars.Keys) {
        [System.Environment]::SetEnvironmentVariable($k, $envVars[$k], "Process")
    }
    Set-Location $wd
    & uv run python -m agent_worker
} -ArgumentList (Join-Path $ROOT "apps\agent-worker"), ([Environment]::GetEnvironmentVariables("Process"))

Write-Host ""
Write-Host "Mathodology is up."
Write-Host "  UI       http://$($env:GATEWAY_HOST):$($env:GATEWAY_PORT)/"
Write-Host "  Gateway  http://$($env:GATEWAY_HOST):$($env:GATEWAY_PORT)/health"
Write-Host "  Auth     Bearer $($env:DEV_AUTH_TOKEN)  (dev token; CHANGE in production)"
Write-Host ""
Write-Host "Press Ctrl-C to stop."

# Stream both jobs' output until either dies; SIGINT triggers finally.
try {
    while ($true) {
        Receive-Job -Job $gatewayJob
        Receive-Job -Job $workerJob
        if ($gatewayJob.State -ne "Running" -or $workerJob.State -ne "Running") { break }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "!! one of the services exited; tearing down the other"
} finally {
    Stop-Job -Job $gatewayJob, $workerJob -ErrorAction SilentlyContinue
    Receive-Job -Job $gatewayJob, $workerJob -ErrorAction SilentlyContinue
    Remove-Job -Job $gatewayJob, $workerJob -Force -ErrorAction SilentlyContinue
}
