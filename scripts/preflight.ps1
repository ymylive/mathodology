# preflight.ps1 — verify Mathodology runtime prereqs on Windows.
#
# Returns exit 0 iff every required tool is present at the minimum version.
# Optional tools are reported as warnings.

[CmdletBinding()]
param()

$script:Fail = 0
$script:Warn = 0

function Get-VersionFromOutput($text) {
    if ($text -match '(\d+(?:\.\d+){1,2})') { return [version]$Matches[1] }
    return $null
}

function Install-Hint($tool) {
    switch ($tool) {
        "python"            { "winget install --id Python.Python.3.11 -e" }
        "uv"                { "winget install --id astral-sh.uv -e   (or: irm https://astral.sh/uv/install.ps1 | iex)" }
        "node"              { "winget install --id OpenJS.NodeJS.LTS -e" }
        "pnpm"              { "corepack enable; corepack prepare pnpm@latest --activate" }
        "redis-cli"         { "Use Memurai Developer (winget install Memurai.MemuraiDeveloper) or run Redis in WSL/Docker" }
        "psql"              { "winget install --id PostgreSQL.PostgreSQL -e   (then add C:\Program Files\PostgreSQL\<v>\bin to PATH)" }
        "pandoc"            { "winget install --id JohnMacFarlane.Pandoc -e" }
        "tectonic"          { "winget install --id Tectonic.Tectonic -e   (or: scoop install tectonic)" }
        "open-websearch"    { "npm i -g open-websearch" }
        default             { "see docs/install/windows.md" }
    }
}

function Check-Tool {
    param(
        [string]$Bin,
        [version]$MinVersion,
        [ValidateSet("required","optional")][string]$Tier,
        [string]$VersionArg = "--version"
    )
    $cmd = Get-Command $Bin -ErrorAction SilentlyContinue
    if (-not $cmd) {
        $hint = Install-Hint $Bin
        if ($Tier -eq "required") {
            "[ MISS ] {0,-15} not found        — install: {1}" -f $Bin, $hint | Write-Host -ForegroundColor Red
            $script:Fail++
        } else {
            "[ MISS ] {0,-15} not found (optional) — install: {1}" -f $Bin, $hint | Write-Host -ForegroundColor Yellow
            $script:Warn++
        }
        return
    }
    try { $output = & $Bin $VersionArg 2>&1 | Out-String } catch { $output = "" }
    $found = Get-VersionFromOutput $output
    if (-not $found) {
        "[ ?    ] {0,-15} installed (version unknown)" -f $Bin | Write-Host
        return
    }
    if ($found -ge $MinVersion) {
        "[ OK   ] {0,-15} {1}" -f $Bin, $found | Write-Host -ForegroundColor Green
    } else {
        $hint = Install-Hint $Bin
        if ($Tier -eq "required") {
            "[ STALE] {0,-15} {1} < {2}   — upgrade: {3}" -f $Bin, $found, $MinVersion, $hint | Write-Host -ForegroundColor Red
            $script:Fail++
        } else {
            "[ STALE] {0,-15} {1} < {2} (optional) — upgrade: {3}" -f $Bin, $found, $MinVersion, $hint | Write-Host -ForegroundColor Yellow
            $script:Warn++
        }
    }
}

Write-Host "Mathodology preflight (windows)"
Write-Host "------------------------------------------------------------"

# Required. Try python3.11 first, then python.
if (Get-Command "python3.11" -ErrorAction SilentlyContinue) {
    Check-Tool -Bin "python3.11" -MinVersion ([version]"3.11.0") -Tier "required"
} else {
    Check-Tool -Bin "python"     -MinVersion ([version]"3.11.0") -Tier "required"
}
Check-Tool -Bin "uv"        -MinVersion ([version]"0.4.0")  -Tier "required"
Check-Tool -Bin "redis-cli" -MinVersion ([version]"6.0.0")  -Tier "required"
Check-Tool -Bin "psql"      -MinVersion ([version]"14.0")   -Tier "required"
Check-Tool -Bin "pandoc"    -MinVersion ([version]"2.0")    -Tier "required"

# Optional.
Check-Tool -Bin "tectonic"        -MinVersion ([version]"0.14") -Tier "optional"
Check-Tool -Bin "node"            -MinVersion ([version]"20.0") -Tier "optional"
Check-Tool -Bin "pnpm"            -MinVersion ([version]"9.0")  -Tier "optional"
Check-Tool -Bin "open-websearch"  -MinVersion ([version]"0.0")  -Tier "optional"

Write-Host "------------------------------------------------------------"
if ($script:Fail -gt 0) {
    Write-Host "FAIL: $script:Fail required tool(s) missing or stale. See hints above." -ForegroundColor Red
    Write-Host "Tip: scripts\install.ps1 installs everything via winget."
    exit 1
}
if ($script:Warn -gt 0) {
    Write-Host "OK with $script:Warn optional tool(s) missing (some features may degrade)." -ForegroundColor Yellow
} else {
    Write-Host "OK — all checks passed." -ForegroundColor Green
}
exit 0
