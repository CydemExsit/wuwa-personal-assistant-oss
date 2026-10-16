[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:QT_QPA_PLATFORM = "offscreen"

function Invoke-CheckedNative {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Python environment is missing. Run .\scripts\setup.ps1 first."
}

Push-Location $repoRoot
try {
    # Keep top-level QApplication startup checks in their own native process.  On
    # Windows, reusing a Qt runtime after hundreds of offscreen widget teardown
    # cycles can crash inside the platform style plugin before pytest can report.
    # Both invocations together still execute the complete suite.
    Invoke-CheckedNative $venvPython @(
        "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests\test_app_startup.py"
    )
    Invoke-CheckedNative $venvPython @(
        "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests",
        "--ignore=tests/test_app_startup.py"
    )
    Invoke-CheckedNative $venvPython @("-B", "-m", "ruff", "check", ".")
    Invoke-CheckedNative $venvPython @("-B", "-m", "ruff", "format", "--check", ".")
    Invoke-CheckedNative $venvPython @("-B", "-m", "mypy", "src")
    Invoke-CheckedNative $venvPython @("-m", "pip", "check")
    Invoke-CheckedNative $venvPython @("-m", "wuwa_assistant.safety_scan")
    Invoke-CheckedNative $venvPython @("scripts/public_safety_audit.py")

    $git = Get-Command "git.exe" -ErrorAction Stop
    Invoke-CheckedNative $git.Source @("diff", "--check")
    Write-Host "All tests and safety checks passed."
}
finally {
    Pop-Location
}
