[CmdletBinding()]
param(
    [string]$DataRoot,
    [switch]$SmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Python environment is missing. Run .\scripts\setup.ps1 first."
}

$launchArguments = @("-m", "wuwa_assistant.app")
if ($DataRoot) {
    $launchArguments += @("--data-root", [System.IO.Path]::GetFullPath($DataRoot))
}
if ($SmokeTest) {
    $launchArguments += "--smoke-test"
    $env:QT_QPA_PLATFORM = "offscreen"
}

Push-Location $repoRoot
try {
    & $venvPython @launchArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Application exited with code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
