[CmdletBinding()]
param(
    [switch]$SkipQualityChecks,
    [switch]$SkipDistribution,
    [string]$OutputDirectory = "dist"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"

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

function Invoke-PackagedCheck {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(120000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "Packaged $Description exceeded the 120-second timeout."
    }
    if ($process.ExitCode -ne 0) {
        throw "Packaged $Description exited with code $($process.ExitCode)"
    }
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$specPath = Join-Path $repoRoot "packaging\WuwaDisplayAssistant.spec"
$allowedDist = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "dist"))
$outputRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
if (($outputRoot -ne $allowedDist) -and
    -not $outputRoot.StartsWith($allowedDist + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Build output must stay within the repository dist directory: $outputRoot"
}
$distRoot = Join-Path $outputRoot "WuwaDisplayAssistant"
$executable = Join-Path $distRoot "WuwaDisplayAssistant.exe"
$reportRoot = Join-Path $outputRoot "reports"
$auditScript = Join-Path $PSScriptRoot "package_audit.py"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Python environment is missing. Run .\scripts\setup.ps1 first."
}
if (-not (Test-Path -LiteralPath $specPath -PathType Leaf)) {
    throw "PyInstaller specification is missing: $specPath"
}

Push-Location $repoRoot
try {
    Invoke-CheckedNative $venvPython @(
        "-c",
        "import PyInstaller; from rapidocr import __file__ as f; assert f"
    )
    if (-not $SkipQualityChecks) {
        & (Join-Path $PSScriptRoot "test.ps1")
        if (-not $?) {
            throw "Quality checks failed."
        }
    }
    Invoke-CheckedNative $venvPython @(
        (Join-Path $PSScriptRoot "third_party_sources.py"), "--output-directory", $outputRoot
    )
    Invoke-CheckedNative $venvPython @($auditScript, "--report-directory", $reportRoot)
    # A failed rebuild must not leave an earlier success report in the artifact.
    foreach ($reportName in @("package-audit.json", "package-validation.json")) {
        $oldReport = Join-Path $reportRoot $reportName
        if (Test-Path -LiteralPath $oldReport -PathType Leaf) {
            Remove-Item -LiteralPath $oldReport -Force
        }
    }

    # Native tools on the caller's PATH can carry incompatible DLLs with the same
    # names as Windows/Qt dependencies (for example Poppler's ICU). Keep binary
    # discovery local to this Python environment and Windows, then restore PATH
    # even if PyInstaller fails. Package smoke tests use the caller's PATH again.
    $basePythonRoot = & $venvPython -c "import sys; print(sys.base_prefix)"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not resolve the virtual environment's base Python directory."
    }
    $buildSearchPath = @(
        (Split-Path -Parent $venvPython),
        $basePythonRoot,
        (Join-Path $basePythonRoot "DLLs"),
        [System.Environment]::SystemDirectory,
        [System.Environment]::GetFolderPath("Windows")
    ) -join [System.IO.Path]::PathSeparator
    $callerPath = $env:PATH
    try {
        $env:PATH = $buildSearchPath
        Invoke-CheckedNative $venvPython @(
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            $outputRoot,
            $specPath
        )
    }
    finally {
        $env:PATH = $callerPath
    }

    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "Packaged executable is missing: $executable"
    }
    Invoke-CheckedNative $venvPython @(
        $auditScript,
        "--report-directory", $reportRoot,
        "--package-root", $distRoot,
        "--analysis-toc", (Join-Path $repoRoot "build\WuwaDisplayAssistant\Analysis-00.toc")
    )
    Invoke-PackagedCheck $executable @("--ocr-self-test") "offline OCR self-test"

    $temporaryBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $smokeRoot = Join-Path $temporaryBase ("WuwaDisplayAssistant-Smoke-" + [guid]::NewGuid())
    [System.IO.Directory]::CreateDirectory($smokeRoot) | Out-Null
    $previousQtPlatform = $env:QT_QPA_PLATFORM
    try {
        $env:QT_QPA_PLATFORM = "offscreen"
        Invoke-PackagedCheck $executable @("--smoke-test", "--data-root", "`"$smokeRoot`"") "startup smoke test"
    }
    finally {
        $env:QT_QPA_PLATFORM = $previousQtPlatform
        $resolvedSmokeRoot = [System.IO.Path]::GetFullPath($smokeRoot)
        $safePrefix = $temporaryBase.TrimEnd("\") + "\WuwaDisplayAssistant-Smoke-"
        if ($resolvedSmokeRoot.StartsWith($safePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedSmokeRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
        else {
            throw "Refusing to remove unexpected smoke-test path: $resolvedSmokeRoot"
        }
    }

    @{
        status = "passed"
        offline_ocr = "passed (synthetic image only)"
        startup = "passed (isolated temporary data root, capture disabled)"
        source_and_resource_audit = "passed"
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $reportRoot "package-validation.json") -Encoding UTF8
    if (-not $SkipDistribution) {
        & (Join-Path $PSScriptRoot "build-installer.ps1") -OutputDirectory $outputRoot
        if (-not $?) { throw "Installer and portable distribution failed." }
    }
    Write-Host "Windows one-folder build ready at $distRoot; verification reports at $reportRoot"
}
finally {
    Pop-Location
}
