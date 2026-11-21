<#
Runs the dashboard test suite from the project folder.

    .\run-tests.ps1            # everything
    .\run-tests.ps1 -Live      # only the tests that read the live database
    .\run-tests.ps1 -Scratch   # only the tests that use a temp database
    .\run-tests.ps1 -Verbose   # full pytest output
#>
param(
    [switch]$Live,
    [switch]$Scratch,
    [switch]$Verbose
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$args = @("tests")
if ($Live) { $args += "-m"; $args += "live" }
elseif ($Scratch) { $args += "-m"; $args += "scratch" }
if ($Verbose) { $args += "-vv" } else { $args += "-q" }

Write-Host "OpenCode dashboard tests" -ForegroundColor Cyan
Write-Host "  project : $root"
Write-Host "  marker  : $(if ($Live) { 'live' } elseif ($Scratch) { 'scratch' } else { 'all' })"

python -m pytest @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
