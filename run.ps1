<#
Starts the OpenCode dashboard.

    .\run.ps1              # start on http://127.0.0.1:8791
    .\run.ps1 -Port 9000   # different port
    .\run.ps1 -ReadOnly    # refuse mutating endpoints
#>
param(
    [int]$Port = 8791,
    [string]$HostAddress = "127.0.0.1",
    [switch]$ReadOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$env:DASHBOARD_PORT = "$Port"
$env:DASHBOARD_HOST = $HostAddress
if ($ReadOnly) { $env:DASHBOARD_READ_ONLY = "1" }

# ask the app where the database is, so OPENCODE_DB / OPENCODE_DATA_DIR and
# the platform defaults are respected
$dbPath = python -c "from dashboard.config import load_settings; print(load_settings().db_path)"

Write-Host "OpenCode dashboard" -ForegroundColor Cyan
Write-Host "  database : $dbPath"
Write-Host "  listening: http://${HostAddress}:${Port}" -ForegroundColor Green

python -m dashboard.app
