# Remove Mathodology Windows services. Idempotent — silent if already gone.

[CmdletBinding()]
param(
    [string]$LogDir = "C:\ProgramData\Mathodology\logs",
    [switch]$KeepLogs
)

$ErrorActionPreference = "Stop"

# ---------- 1. admin guard ----------
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "!! Must run from an elevated (Administrator) PowerShell."
}

$nssm = (Get-Command nssm.exe -ErrorAction SilentlyContinue)?.Source
if (-not $nssm) {
    Write-Error "!! nssm.exe not on PATH. Install with: winget install NSSM.NSSM"
}

# ---------- 2. stop + remove (worker first to release gateway dep) ----------
foreach ($svc in @("Mathodology-Worker", "Mathodology-Gateway")) {
    if (Get-Service -Name $svc -ErrorAction SilentlyContinue) {
        Write-Host "==> stopping $svc"
        & $nssm stop $svc confirm 2>$null | Out-Null
        Write-Host "==> removing $svc"
        & $nssm remove $svc confirm | Out-Null
    } else {
        Write-Host "==> $svc not installed, skipping"
    }
}

# ---------- 3. logs ----------
if (-not $KeepLogs -and (Test-Path $LogDir)) {
    Write-Host "==> removing logs at $LogDir (use -KeepLogs to retain)"
    Remove-Item -Recurse -Force -LiteralPath $LogDir
}

Write-Host ""
Write-Host "Uninstalled. Verify:"
Write-Host "  Get-Service Mathodology-Gateway, Mathodology-Worker -ErrorAction SilentlyContinue"
Write-Host "  (should print nothing)"
