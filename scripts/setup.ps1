[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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
$venvRoot = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"

Push-Location $repoRoot
try {
    $launcher = Get-Command "py.exe" -ErrorAction Stop
    Invoke-CheckedNative $launcher.Source @(
        "-3.12",
        "-c",
        "import sys; assert sys.version_info[:2] == (3, 12), sys.version"
    )

    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Write-Host "Creating Python 3.12 virtual environment at $venvRoot"
        Invoke-CheckedNative $launcher.Source @("-3.12", "-m", "venv", $venvRoot)
    }

    Invoke-CheckedNative $venvPython @(
        "-c",
        "import sys; assert sys.version_info[:2] == (3, 12), sys.version"
    )
    # ORT distributions share the same Python module/DLL paths. Remove all
    # overlapping variants before upgrading an existing CPU/CUDA environment.
    $ortConflict = & $venvPython -c "from importlib.metadata import distributions; print(any(d.metadata['Name'].lower() in {'onnxruntime', 'onnxruntime-gpu'} for d in distributions()))"
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect installed OCR runtime." }
    if ($ortConflict -eq "True") {
        Invoke-CheckedNative $venvPython @(
            "-m", "pip", "uninstall", "-y", "onnxruntime", "onnxruntime-gpu", "onnxruntime-directml"
        )
    }
    Invoke-CheckedNative $venvPython @(
        "-m", "pip", "install", "--require-hashes", "-r", "requirements/windows-py312.lock"
    )
    Invoke-CheckedNative $venvPython @(
        "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", "."
    )
    Invoke-CheckedNative $venvPython @("-m", "pip", "check")
    Invoke-CheckedNative $venvPython @("-m", "wuwa_assistant.safety_scan")

    Write-Host "Setup complete. Start the app with .\scripts\run.ps1"
}
finally {
    Pop-Location
}
