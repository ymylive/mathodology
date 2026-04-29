# install.ps1 — first-time machine setup for Mathodology (Windows).
#
# Companion to scripts\preflight.ps1: same tool list, same install hints, but
# this script actually runs the install commands. Idempotent — safe to re-run.
# Prefers winget; falls back to scoop when winget can't satisfy a package.
#
# Usage:
#   scripts\install.ps1                # interactive, runtime tools only
#   scripts\install.ps1 -Yes           # no prompt (CI / scripted)
#   scripts\install.ps1 -WithSource    # also install node + pnpm (for SOURCE builds)
#
# Release-archive users do NOT need -WithSource; the gateway binary and
# prebuilt SPA ship in the archive.

[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$WithSource
)

$ErrorActionPreference = "Stop"

$script:Installed = New-Object System.Collections.ArrayList
$script:Skipped   = New-Object System.Collections.ArrayList
$script:Failed    = New-Object System.Collections.ArrayList

function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# Detect package managers up front. winget is the primary; scoop is fallback
# for tools winget doesn't ship cleanly (currently: tectonic on some channels).
$HasWinget = Have "winget"
$HasScoop  = Have "scoop"

if (-not $HasWinget -and -not $HasScoop) {
    Write-Host "!! Neither winget nor scoop is available." -ForegroundColor Red
    Write-Host "   winget ships with Windows 10 1809+/Windows 11. Update App Installer from the Microsoft Store,"
    Write-Host "   or install scoop: irm get.scoop.sh | iex"
    exit 1
}

# ---------- 1. plan + confirm ----------
$plan = New-Object System.Collections.ArrayList
if (-not (Have "python3.11") -and -not (Have "python")) { [void]$plan.Add("python 3.11") }
if (-not (Have "uv"))         { [void]$plan.Add("uv") }
if (-not (Have "redis-cli"))  { [void]$plan.Add("redis (Memurai Developer)") }
if (-not (Have "psql"))       { [void]$plan.Add("postgresql 14+") }
if (-not (Have "pandoc"))     { [void]$plan.Add("pandoc") }
if (-not (Have "tectonic"))   { [void]$plan.Add("tectonic (optional, for PDF export)") }
if ($WithSource) {
    if (-not (Have "node")) { [void]$plan.Add("node 20") }
    if (-not (Have "pnpm")) { [void]$plan.Add("pnpm 9") }
}
if (-not (Have "open-websearch")) { [void]$plan.Add("open-websearch (optional MCP search)") }

$pmLabel = if ($HasWinget) { "winget" } else { "scoop" }
Write-Host "Mathodology install (windows, pkg manager: $pmLabel)"
Write-Host "------------------------------------------------------------"
if ($plan.Count -eq 0) {
    Write-Host "Everything is already installed. Will only verify Postgres/Redis state."
} else {
    Write-Host "Will install:"
    foreach ($p in $plan) { Write-Host "  - $p" }
}
Write-Host "Will then: start Redis service, create Postgres role+db (mm/mm/mm if missing)."
if (-not $WithSource) { Write-Host "Skipping node/pnpm (pass -WithSource to include them)." }
Write-Host "------------------------------------------------------------"

if (-not $Yes) {
    $reply = Read-Host "Proceed? [Y/n]"
    if ($reply -match '^(n|no)$') { Write-Host "aborted."; exit 0 }
}

# winget exits non-zero with code 0x8A15002B when the package is already
# installed — treat that as success. Same idea for scoop ("is already installed").
function Invoke-Winget {
    param([string]$Id, [string]$Pretty)
    $output = & winget install --id $Id -e --silent --accept-source-agreements --accept-package-agreements 2>&1 | Out-String
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        [void]$script:Installed.Add($Pretty); return $true
    }
    # 0x8A15002B = APPINSTALLER_CLI_ERROR_PACKAGE_ALREADY_INSTALLED
    if ($output -match 'already installed' -or $code -eq -1978335189) {
        [void]$script:Skipped.Add("$Pretty (already installed via winget)"); return $true
    }
    return $false
}

function Invoke-Scoop {
    param([string]$Pkg, [string]$Pretty)
    if (-not $HasScoop) { return $false }
    $output = & scoop install $Pkg 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0 -or $output -match 'is already installed') {
        [void]$script:Installed.Add("$Pretty (via scoop)"); return $true
    }
    return $false
}

# ---------- 2. installers ----------
# Each installer: skip if present → try winget → fall back to scoop → record.

function Install-Python {
    if ((Have "python3.11") -or (Have "python")) { [void]$script:Skipped.Add("python"); return }
    if ($HasWinget -and (Invoke-Winget "Python.Python.3.11" "python 3.11")) { return }
    if (Invoke-Scoop "python311" "python 3.11") { return }
    [void]$script:Failed.Add("python — winget install --id Python.Python.3.11 -e")
}

function Install-Uv {
    if (Have "uv") { [void]$script:Skipped.Add("uv"); return }
    if ($HasWinget -and (Invoke-Winget "astral-sh.uv" "uv")) { return }
    # Fall back to astral.sh installer; this is the canonical path on Windows.
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        [void]$script:Installed.Add("uv (via astral.sh installer)")
    } catch {
        [void]$script:Failed.Add("uv — irm https://astral.sh/uv/install.ps1 | iex")
    }
}

function Install-Redis {
    # Native Windows Redis is unmaintained; Memurai is the production-grade
    # Redis-compatible server for Windows. WSL/Docker also work.
    if (Have "redis-cli") { [void]$script:Skipped.Add("redis"); return }
    if ($HasWinget -and (Invoke-Winget "Memurai.MemuraiDeveloper" "Memurai Developer (Redis-compatible)")) { return }
    [void]$script:Failed.Add("redis — install Memurai Developer from https://www.memurai.com/get-memurai or run Redis in WSL/Docker")
}

function Start-RedisService {
    # Memurai installs as a Windows service named 'Memurai'.
    $svc = Get-Service -Name "Memurai" -ErrorAction SilentlyContinue
    if ($svc) {
        if ($svc.Status -ne "Running") {
            try { Start-Service -Name "Memurai"; [void]$script:Installed.Add("Memurai service started") } catch {
                [void]$script:Failed.Add("Memurai service — run as admin: Start-Service Memurai")
            }
        } else {
            [void]$script:Skipped.Add("Memurai service (already running)")
        }
    }
}

function Install-Postgres {
    if (Have "psql") { [void]$script:Skipped.Add("postgresql"); return }
    if ($HasWinget -and (Invoke-Winget "PostgreSQL.PostgreSQL" "postgresql")) {
        Write-Host "   Note: add C:\Program Files\PostgreSQL\<version>\bin to PATH so 'psql' resolves."
        return
    }
    if (Invoke-Scoop "postgresql" "postgresql") { return }
    [void]$script:Failed.Add("postgresql — winget install --id PostgreSQL.PostgreSQL -e")
}

function Start-PostgresService {
    # Installer registers a service named like 'postgresql-x64-16'.
    $svc = Get-Service | Where-Object { $_.Name -like "postgresql*" } | Select-Object -First 1
    if ($svc) {
        if ($svc.Status -ne "Running") {
            try { Start-Service -Name $svc.Name; [void]$script:Installed.Add("$($svc.Name) service started") } catch {
                [void]$script:Failed.Add("$($svc.Name) — run as admin: Start-Service $($svc.Name)")
            }
        } else {
            [void]$script:Skipped.Add("$($svc.Name) service (already running)")
        }
    }
}

# psql may have been installed but isn't on PATH yet. Probe the standard
# install dirs so first-run db setup works without a logout/login cycle.
function Resolve-Psql {
    if (Have "psql") { return (Get-Command psql).Source }
    foreach ($v in 17,16,15,14) {
        $p = "C:\Program Files\PostgreSQL\$v\bin\psql.exe"
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Setup-PostgresDb {
    $psqlExe = Resolve-Psql
    if (-not $psqlExe) {
        [void]$script:Failed.Add("postgres db setup (psql not on PATH; reopen shell after install)")
        return
    }
    # Wait briefly for service to accept connections.
    for ($i = 0; $i -lt 10; $i++) {
        $r = & $psqlExe -U postgres -d postgres -tAc "SELECT 1" 2>$null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 1
    }

    # On Windows the default superuser is 'postgres' with the password set
    # during install. We can't know it; if the probe failed, surface manual cmds.
    if ($LASTEXITCODE -ne 0) {
        [void]$script:Failed.Add("postgres db setup — couldn't connect as 'postgres'. Run manually:")
        [void]$script:Failed.Add("    psql -U postgres -c ""CREATE ROLE mm WITH LOGIN PASSWORD 'mm';""")
        [void]$script:Failed.Add("    psql -U postgres -c ""CREATE DATABASE mm OWNER mm;""")
        return
    }

    $hasRole = & $psqlExe -U postgres -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='mm'" 2>$null
    $hasDb   = & $psqlExe -U postgres -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='mm'" 2>$null

    if ($hasRole.Trim() -ne "1") {
        & $psqlExe -U postgres -d postgres -c "CREATE ROLE mm WITH LOGIN PASSWORD 'mm';" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { [void]$script:Installed.Add("postgres role 'mm'") }
        else { [void]$script:Failed.Add("postgres role 'mm' — run: psql -U postgres -c ""CREATE ROLE mm WITH LOGIN PASSWORD 'mm';""") ; return }
    } else {
        [void]$script:Skipped.Add("postgres role 'mm' (exists)")
    }

    if ($hasDb.Trim() -ne "1") {
        & $psqlExe -U postgres -d postgres -c "CREATE DATABASE mm OWNER mm;" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { [void]$script:Installed.Add("postgres database 'mm'") }
        else { [void]$script:Failed.Add("postgres database 'mm' — run: psql -U postgres -c ""CREATE DATABASE mm OWNER mm;""") }
    } else {
        [void]$script:Skipped.Add("postgres database 'mm' (exists)")
    }
}

function Install-Pandoc {
    if (Have "pandoc") { [void]$script:Skipped.Add("pandoc"); return }
    if ($HasWinget -and (Invoke-Winget "JohnMacFarlane.Pandoc" "pandoc")) { return }
    if (Invoke-Scoop "pandoc" "pandoc") { return }
    [void]$script:Failed.Add("pandoc — winget install --id JohnMacFarlane.Pandoc -e")
}

function Install-Tectonic {
    # Optional: PDF export degrades gracefully without it.
    if (Have "tectonic") { [void]$script:Skipped.Add("tectonic"); return }
    if ($HasWinget -and (Invoke-Winget "Tectonic.Tectonic" "tectonic")) { return }
    if (Invoke-Scoop "tectonic" "tectonic") { return }
    [void]$script:Skipped.Add("tectonic (optional; PDF export disabled — try 'scoop install tectonic' or 'cargo install tectonic')")
}

function Install-NodePnpm {
    if (Have "node") {
        [void]$script:Skipped.Add("node")
    } else {
        if ($HasWinget -and (Invoke-Winget "OpenJS.NodeJS.LTS" "node 20 LTS")) {}
        elseif (Invoke-Scoop "nodejs-lts" "node 20 LTS") {}
        else { [void]$script:Failed.Add("node — winget install --id OpenJS.NodeJS.LTS -e") }
    }
    if (Have "pnpm") {
        [void]$script:Skipped.Add("pnpm")
    } else {
        # corepack ships with node 20+; same path as Linux/macOS.
        try {
            & corepack enable 2>&1 | Out-Null
            & corepack prepare pnpm@latest --activate 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { [void]$script:Installed.Add("pnpm (via corepack)") }
            else { [void]$script:Failed.Add("pnpm — corepack enable; corepack prepare pnpm@latest --activate") }
        } catch {
            [void]$script:Failed.Add("pnpm — corepack enable; corepack prepare pnpm@latest --activate")
        }
    }
}

function Install-OpenWebSearch {
    if (Have "open-websearch") { [void]$script:Skipped.Add("open-websearch"); return }
    if (-not (Have "npm")) {
        [void]$script:Skipped.Add("open-websearch (npm not installed; pass -WithSource or install node first)")
        return
    }
    & npm i -g open-websearch 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { [void]$script:Installed.Add("open-websearch") }
    else { [void]$script:Skipped.Add("open-websearch (npm install failed; optional — try elevated shell)") }
}

# ---------- 3. run ----------
Install-Python
Install-Uv
Install-Redis;    Start-RedisService
Install-Postgres; Start-PostgresService; Setup-PostgresDb
Install-Pandoc
Install-Tectonic
if ($WithSource) { Install-NodePnpm }
Install-OpenWebSearch

# ---------- 4. summary ----------
Write-Host ""
Write-Host "------------------------------------------------------------"
Write-Host "Install summary"
Write-Host "------------------------------------------------------------"
if ($script:Installed.Count -gt 0) {
    Write-Host "Installed:" -ForegroundColor Green
    foreach ($x in $script:Installed) { Write-Host "  + $x" }
}
if ($script:Skipped.Count -gt 0) {
    Write-Host "Skipped (already present or optional):"
    foreach ($x in $script:Skipped) { Write-Host "  . $x" }
}
if ($script:Failed.Count -gt 0) {
    Write-Host "Failed (action required):" -ForegroundColor Red
    foreach ($x in $script:Failed) { Write-Host "  ! $x" }
    Write-Host ""
    Write-Host "See docs\install\windows.md for manual steps."
}
Write-Host ""
Write-Host "Next: run scripts\preflight.ps1 to verify, then .\run.ps1 to start Mathodology."
if ($script:Failed.Count -gt 0) { exit 1 }
exit 0
