# Install Mathodology Windows services (gateway + worker) via NSSM. Idempotent.
#
# Why NSSM (not New-Service / sc.exe):
#   gateway.exe is a console binary and the worker is `uv run python -m
#   agent_worker` — neither calls StartServiceCtrlDispatcher, so registering
#   them as native Windows services results in error 1053 ("did not respond
#   to start request"). NSSM wraps console apps, redirects stdio to log
#   files, and handles graceful shutdown.
#
# Prereq:  winget install NSSM.NSSM    (or download from nssm.cc and add to PATH)
# Run as: Administrator PowerShell, from the install dir or with -InstallDir set.

[CmdletBinding()]
param(
    [string]$InstallDir = "C:\Program Files\Mathodology",
    [string]$LogDir     = "C:\ProgramData\Mathodology\logs"
)

$ErrorActionPreference = "Stop"

# ---------- 1. admin guard ----------
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "!! Must run from an elevated (Administrator) PowerShell."
}

# ---------- 2. NSSM check ----------
$nssm = (Get-Command nssm.exe -ErrorAction SilentlyContinue)?.Source
if (-not $nssm) {
    Write-Error "!! nssm.exe not on PATH. Install with: winget install NSSM.NSSM"
}
Write-Host "==> using NSSM at $nssm"

# ---------- 3. layout checks ----------
$gateway = Join-Path $InstallDir "gateway.exe"
$envFile = Join-Path $InstallDir ".env"
$workerWd = Join-Path $InstallDir "apps\agent-worker"

if (-not (Test-Path $gateway))  { Write-Error "!! $gateway missing — extract release archive first." }
if (-not (Test-Path $envFile))  { Write-Error "!! $envFile missing — copy .env.example and fill in keys." }
if (-not (Test-Path $workerWd)) { Write-Error "!! $workerWd missing — corrupt archive?" }

# ---------- 4. dev token guard ----------
$envLines = Get-Content -LiteralPath $envFile
if ($envLines | Where-Object { $_ -match '^\s*DEV_AUTH_TOKEN\s*=\s*dev-local-insecure-token\s*$' }) {
    Write-Error "!! DEV_AUTH_TOKEN is still 'dev-local-insecure-token'. Set a strong value in $envFile (e.g. [guid]::NewGuid().Guid)."
}

# ---------- 5. resolve `uv` for the worker ----------
$uv = (Get-Command uv.exe -ErrorAction SilentlyContinue)?.Source
if (-not $uv) {
    Write-Error "!! uv.exe not on PATH. Install: irm https://astral.sh/uv/install.ps1 | iex"
}
Write-Host "==> using uv at $uv"

# ---------- 6. log dir ----------
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ---------- 7. .env -> NSSM AppEnvironmentExtra ----------
# NSSM wants a NUL-separated list of KEY=VALUE pairs; we pass them via
# `nssm set <svc> AppEnvironmentExtra` which accepts repeated arguments.
function Parse-DotEnv($path) {
    $entries = @()
    foreach ($line in Get-Content -LiteralPath $path) {
        if ($line -match '^\s*#')   { continue }
        if ($line -match '^\s*$')   { continue }
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $k = $Matches[1]; $v = $Matches[2]
            if ($v.Length -ge 2 -and (
                  ($v[0] -eq '"' -and $v[-1] -eq '"') -or
                  ($v[0] -eq "'" -and $v[-1] -eq "'")
                )) {
                $v = $v.Substring(1, $v.Length - 2)
            }
            $entries += "$k=$v"
        }
    }
    return $entries
}
$envEntries = Parse-DotEnv $envFile

# Fill in defaults the same way release-run.ps1 does, only if .env didn't set them.
function Add-Default([string[]]$list, [string]$key, [string]$value) {
    if (-not ($list | Where-Object { $_ -like "$key=*" })) { $list += "$key=$value" }
    return $list
}
$envEntries = Add-Default $envEntries "REDIS_URL"      "redis://127.0.0.1:6379/0"
$envEntries = Add-Default $envEntries "DATABASE_URL"   "postgres://mm:mm@127.0.0.1:5432/mm"
$envEntries = Add-Default $envEntries "GATEWAY_HOST"   "127.0.0.1"
$envEntries = Add-Default $envEntries "GATEWAY_PORT"   "8080"
$envEntries = Add-Default $envEntries "RUNS_DIR"       (Join-Path $InstallDir "runs")
$envEntries = Add-Default $envEntries "STATIC_DIR"     (Join-Path $InstallDir "apps\web\dist")
$envEntries = Add-Default $envEntries "GATEWAY_HTTP"   "http://127.0.0.1:8080"

New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "runs") | Out-Null

# ---------- 8. probe for optional service deps ----------
# DependOnService takes a slash-separated list. Only include services that
# actually exist on this machine — `sc config` errors otherwise.
$deps = @()
foreach ($candidate in @("postgresql-x64-16","postgresql-x64-15","postgresql-x64-14","Redis","Memurai")) {
    if (Get-Service -Name $candidate -ErrorAction SilentlyContinue) { $deps += $candidate }
}
$depString = if ($deps.Count -gt 0) { ($deps -join "/") } else { "" }
Write-Host "==> service dependencies: $($deps -join ', ' )"

# ---------- 9. install/update one service via NSSM (idempotent) ----------
function Set-NssmService {
    param(
        [string]$Name,
        [string]$Exe,
        [string]$Args,
        [string]$WorkingDir,
        [string]$LogPath,
        [string[]]$Env,
        [string]$DependsOn
    )
    $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "==> service $Name exists, updating"
        & $nssm stop  $Name confirm 2>$null | Out-Null
    } else {
        Write-Host "==> installing service $Name"
        & $nssm install $Name $Exe | Out-Null
    }

    & $nssm set $Name Application      $Exe              | Out-Null
    & $nssm set $Name AppParameters    $Args             | Out-Null
    & $nssm set $Name AppDirectory     $WorkingDir       | Out-Null
    & $nssm set $Name DisplayName      "Mathodology - $Name" | Out-Null
    & $nssm set $Name Description      "Mathodology long-running service ($Name)" | Out-Null
    & $nssm set $Name Start            SERVICE_AUTO_START | Out-Null
    & $nssm set $Name AppStdout        $LogPath          | Out-Null
    & $nssm set $Name AppStderr        $LogPath          | Out-Null
    & $nssm set $Name AppRotateFiles   1                 | Out-Null
    & $nssm set $Name AppRotateBytes   10485760          | Out-Null
    & $nssm set $Name AppExit          Default Restart   | Out-Null
    & $nssm set $Name AppRestartDelay  5000              | Out-Null
    # SIGINT-equivalent on Windows: send Ctrl+Break, wait 15s, then kill.
    & $nssm set $Name AppStopMethodConsole 15000         | Out-Null

    # NSSM env: AppEnvironmentExtra accepts multiple :KEY=VAL pairs separated
    # by spaces when called via the CLI, but the most reliable form is one
    # call per pair using `+`. We pass the whole list at once.
    & $nssm set $Name AppEnvironmentExtra $Env           | Out-Null

    if ($DependsOn) {
        & $nssm set $Name DependOnService $DependsOn     | Out-Null
    }
}

Set-NssmService -Name "Mathodology-Gateway" `
                -Exe $gateway `
                -Args "" `
                -WorkingDir $InstallDir `
                -LogPath (Join-Path $LogDir "gateway.log") `
                -Env $envEntries `
                -DependsOn $depString

# Worker: NSSM runs `uv run python -m agent_worker` from apps/agent-worker.
Set-NssmService -Name "Mathodology-Worker" `
                -Exe $uv `
                -Args "run python -m agent_worker" `
                -WorkingDir $workerWd `
                -LogPath (Join-Path $LogDir "worker.log") `
                -Env $envEntries `
                -DependsOn "Mathodology-Gateway$(if ($depString) { '/' + $depString })"

# ---------- 10. start ----------
Write-Host "==> starting services"
Start-Service -Name "Mathodology-Gateway"
Start-Service -Name "Mathodology-Worker"

# ---------- 11. status hint ----------
Write-Host ""
Write-Host "Installed. Verify:"
Write-Host "  Get-Service Mathodology-Gateway, Mathodology-Worker"
Write-Host "  Get-Content -Wait '$LogDir\gateway.log'"
Write-Host "  Get-Content -Wait '$LogDir\worker.log'"
Write-Host "  Invoke-WebRequest http://127.0.0.1:8080/health -UseBasicParsing"
Write-Host ""
Write-Host "Restart:    Restart-Service Mathodology-Gateway, Mathodology-Worker"
Write-Host "Uninstall:  .\uninstall-service.ps1"
