[CmdletBinding()]
param([switch]$Package)

$ErrorActionPreference = "Stop"
$env:QT_QPA_PLATFORM = "offscreen"
& "$PSScriptRoot/setup.ps1"
& "$PSScriptRoot/audit-dependencies.ps1"
& "$PSScriptRoot/scan-secrets.ps1"
& "$PSScriptRoot/test.ps1"
if ($Package) {
    & "$PSScriptRoot/build.ps1" -SkipQualityChecks -OutputDirectory dist/preflight
}
