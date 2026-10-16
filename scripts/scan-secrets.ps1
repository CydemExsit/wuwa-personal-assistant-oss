[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$toolRoot = Join-Path $repoRoot "work\security-tools\gitleaks-8.30.1"
$archive = Join-Path $toolRoot "gitleaks_8.30.1_windows_x64.zip"
$expected = "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e"
New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    Invoke-WebRequest -Uri "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_windows_x64.zip" -OutFile $archive
}
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
    throw "Gitleaks download checksum mismatch."
}
Expand-Archive -LiteralPath $archive -DestinationPath $toolRoot -Force
Push-Location $repoRoot
try {
    & (Join-Path $toolRoot "gitleaks.exe") git . --log-opts="--all" --redact=100 --no-banner --report-format json --report-path work/history-secrets-redacted.json
    if ($LASTEXITCODE -ne 0) { throw "History secret scan did not pass. Inspect the redacted report locally." }
}
finally { Pop-Location }
