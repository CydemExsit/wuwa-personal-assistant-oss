[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
$allowedDist = Join-Path $repoRoot "dist"
if (-not $outputRoot.StartsWith($allowedDist + "\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Installer output must be inside repository dist."
}
$packageRoot = Join-Path $outputRoot "WuwaDisplayAssistant"
if (-not (Test-Path (Join-Path $packageRoot "WuwaDisplayAssistant.exe"))) {
    throw "Build and verify the application package first."
}
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$version = & $python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid project version." }
$edition = Get-Content (Join-Path $repoRoot "packaging\distribution.json") -Raw | ConvertFrom-Json

# Build-only dependency. GitHub Windows runners already provide Inno Setup.
# Do not silently download/install executable build tools on a developer machine.
$compiler = $env:ISCC_EXE
if (-not $compiler) {
    $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($command) { $compiler = $command.Source }
}
if (-not $compiler) {
    foreach ($candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 7\ISCC.exe"
    )) {
        if (Test-Path -LiteralPath $candidate) { $compiler = $candidate; break }
    }
}
if (-not $compiler -or -not (Test-Path -LiteralPath $compiler)) {
    throw "Inno Setup compiler missing. Use Windows CI, or install Inno Setup and set ISCC_EXE."
}
& $compiler /Qp "/DEditionId=$($edition.id)" "/DEditionName=$($edition.name)" `
    "/DAssetName=$($edition.asset)" "/DRepository=$($edition.repository)" `
    "/DAppVersion=$version" "/DSourcePath=$packageRoot" "/DOutputDirectory=$outputRoot" `
    (Join-Path $repoRoot "packaging\installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Installer compiler failed." }
$installer = Join-Path $outputRoot "$($edition.asset)-$version-Setup.exe"
& (Join-Path $PSScriptRoot "verify-installer.ps1") -Installer $installer -PackageRoot $packageRoot
if (-not $?) { throw "Installer verification failed." }

& $python (Join-Path $PSScriptRoot "release_assets.py") --output-directory $outputRoot
if ($LASTEXITCODE -ne 0) { throw "Release asset verification failed." }
