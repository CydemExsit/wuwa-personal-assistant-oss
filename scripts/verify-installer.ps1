[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][string]$PackageRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$edition = Get-Content (Join-Path $repoRoot "packaging\distribution.json") -Raw | ConvertFrom-Json
$registry = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$($edition.id)_is1"
if (Test-Path $registry) { throw "Refusing to overwrite an existing installed edition during verification." }
$testRoot = Join-Path $repoRoot ("work\installer-test-" + [guid]::NewGuid())
$installRoot = Join-Path $testRoot "安裝 程式"
$dataRoot = Join-Path $testRoot "保留 資料"
$group = "WuwaInstallerTest-" + [guid]::NewGuid()
$shortcutRoot = Join-Path ([Environment]::GetFolderPath("Programs")) $group
$desktopLink = Join-Path ([Environment]::GetFolderPath("DesktopDirectory")) "$($edition.name).lnk"
if (Test-Path -LiteralPath $desktopLink) { throw "Refusing to replace an existing desktop shortcut during verification." }
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
function Run-Checked([string]$File, [string[]]$Arguments) {
    $process = Start-Process -FilePath $File -ArgumentList $Arguments -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(120000)) { throw "Installer test timed out." }
    if ($process.ExitCode -ne 0) { throw "Installer test failed: exit $($process.ExitCode)" }
}
$uninstaller = Join-Path $installRoot "unins000.exe"
try {
    $installArgs = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=desktopicon", "/DIR=`"$installRoot`"", "/GROUP=$group")
    Run-Checked $Installer $installArgs
    $exe = Join-Path $installRoot "WuwaDisplayAssistant.exe"
    $sourceFiles = @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -File)
    foreach ($file in $sourceFiles) {
        $relative = $file.FullName.Substring($PackageRoot.Length).TrimStart("\")
        $installed = Join-Path $installRoot $relative
        if (-not (Test-Path -LiteralPath $installed) -or
            (Get-FileHash -LiteralPath $installed).Hash -ne (Get-FileHash -LiteralPath $file.FullName).Hash) {
            throw "Installed file mismatch: $relative"
        }
    }
    $link = Join-Path $shortcutRoot "$($edition.name).lnk"
    $shell = New-Object -ComObject WScript.Shell
    if (-not (Test-Path -LiteralPath $link) -or $shell.CreateShortcut($link).TargetPath -ne $exe) {
        throw "Start-menu shortcut does not target the installed application."
    }
    if (-not (Test-Path -LiteralPath $desktopLink) -or $shell.CreateShortcut($desktopLink).TargetPath -ne $exe) {
        throw "Desktop shortcut does not target the installed application."
    }
    Run-Checked $exe @("--smoke-test", "--data-root", "`"$dataRoot`"")
    Run-Checked $exe @("--ocr-self-test")
    $before = @(Get-ChildItem -LiteralPath $dataRoot -Recurse -File | Get-FileHash | Select-Object Path,Hash)
    Run-Checked $Installer $installArgs
    $reinstalled = @(Get-ChildItem -LiteralPath $dataRoot -Recurse -File | Get-FileHash | Select-Object Path,Hash)
    if ((ConvertTo-Json $before -Compress) -ne (ConvertTo-Json $reinstalled -Compress)) { throw "Reinstall changed application data." }
    Run-Checked $exe @("--smoke-test", "--data-root", "`"$dataRoot`"")
    # Snapshot after the application has closed; uninstall alone must change nothing.
    $before = @(Get-ChildItem -LiteralPath $dataRoot -Recurse -File | Get-FileHash | Select-Object Path,Hash)
    Run-Checked $uninstaller @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
    $after = @(Get-ChildItem -LiteralPath $dataRoot -Recurse -File | Get-FileHash | Select-Object Path,Hash)
    if ((ConvertTo-Json $before -Compress) -ne (ConvertTo-Json $after -Compress)) { throw "Uninstall changed application data." }
    if ((Test-Path -LiteralPath $exe) -or (Test-Path -LiteralPath $link) -or (Test-Path -LiteralPath $desktopLink) -or (Test-Path $registry)) {
        throw "Uninstall left an executable, shortcut or registration."
    }
    @{
        status = "passed"; installed_files = $sourceFiles.Count; shortcut = "passed"; desktop_shortcut = "passed"
        installed_startup = "passed"; offline_ocr = "passed"; reinstall = "passed"
        uninstall = "passed"; data_preserved = "passed"; unicode_space_path = "passed"
    } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path (Split-Path $Installer) "reports\installer-validation.json")
}
finally {
    if (Test-Path -LiteralPath $uninstaller) {
        Run-Checked $uninstaller @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
    }
    # Evidence and isolated data remain in ignored work/, never remove user data.
}
