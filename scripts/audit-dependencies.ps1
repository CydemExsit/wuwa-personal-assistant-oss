[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Push-Location $repoRoot
try {
    $auditEnv = Join-Path $repoRoot "work/ci-audit-env"
    $auditPython = Join-Path $auditEnv "Scripts/python.exe"
    if (-not (Test-Path -LiteralPath $auditPython)) {
        & py -3.12 -m venv $auditEnv
        if ($LASTEXITCODE -ne 0) { throw "Unable to create dependency audit environment." }
    }
    & $auditPython -m pip install --require-hashes -r requirements/audit-windows-py312.lock
    if ($LASTEXITCODE -ne 0) { throw "Unable to install locked dependency audit tools." }
    & $auditPython -m pip_audit --requirement requirements/windows-py312.lock --no-deps --disable-pip
    if ($LASTEXITCODE -ne 0) { throw "Dependency audit did not pass." }
}
finally {
    Pop-Location
}
