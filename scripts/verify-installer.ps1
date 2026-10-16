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
# DisableProgramGroupPage=yes ignores /GROUP; verify the real default group.
$group = $edition.name
$shortcutRoot = Join-Path ([Environment]::GetFolderPath("Programs")) $group
if (Test-Path -LiteralPath $shortcutRoot) { throw "Refusing to replace an existing Start-menu folder during verification." }
$desktopLink = Join-Path ([Environment]::GetFolderPath("DesktopDirectory")) "$($edition.name).lnk"
if (Test-Path -LiteralPath $desktopLink) { throw "Refusing to replace an existing desktop shortcut during verification." }
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
function Run-Checked([string]$File, [string[]]$Arguments) {
    $process = Start-Process -FilePath $File -ArgumentList $Arguments -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(120000)) { throw "Installer test timed out." }
    if ($process.ExitCode -ne 0) { throw "Installer test failed: $(Split-Path $File -Leaf), exit $($process.ExitCode)" }
}
$uninstaller = Join-Path $installRoot "unins000.exe"
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
function Assert-Shortcut([string]$Path, [string]$Expected) {
    if (-not (Test-Path -LiteralPath $Path)) {
        $names = @(Get-ChildItem -LiteralPath (Split-Path $Path) -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
        throw "Shortcut missing: $(Split-Path $Path -Leaf). Available names: $($names -join ', ')"
    }
    # WScript's legacy reader can return an empty ANSI target on an English
    # Windows host for a Unicode installation path. Use the native Shell object.
    $shell = New-Object -ComObject Shell.Application
    $folder = $shell.NameSpace((Split-Path $Path))
    $item = $folder.ParseName((Split-Path $Path -Leaf))
    $target = $item.GetLink.Path
    if (-not $target) { throw "Windows Shell could not resolve the shortcut target." }
    # Compare file identity so valid Windows path aliases remain acceptable.
    & $python -c "import os,sys; assert os.path.samefile(sys.argv[1],sys.argv[2])" $target $Expected
    if ($LASTEXITCODE -ne 0) { throw "Shortcut target is not the installed executable." }
}
$uninstalled = $false
try {
    $installArgs = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR=`"$installRoot`"")
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
    Assert-Shortcut $link $exe
    Assert-Shortcut $desktopLink $exe
    Run-Checked $link @("--smoke-test", "--data-root", "`"$dataRoot`"")
    Run-Checked $exe @("--ocr-self-test")
    $before = @(Get-ChildItem -LiteralPath $dataRoot -Recurse -File | Get-FileHash | Select-Object Path,Hash)
    Run-Checked $Installer $installArgs
    $reinstalled = @(Get-ChildItem -LiteralPath $dataRoot -Recurse -File | Get-FileHash | Select-Object Path,Hash)
    if ((ConvertTo-Json $before -Compress) -ne (ConvertTo-Json $reinstalled -Compress)) { throw "Reinstall changed application data." }
    Run-Checked $desktopLink @("--smoke-test", "--data-root", "`"$dataRoot`"")
    # Snapshot after the application has closed; uninstall alone must change nothing.
    $before = @(Get-ChildItem -LiteralPath $dataRoot -Recurse -File | Get-FileHash | Select-Object Path,Hash)
    Run-Checked $uninstaller @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
    $uninstalled = $true
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
    if (-not $uninstalled -and (Test-Path -LiteralPath $uninstaller) -and (Test-Path $registry)) {
        Run-Checked $uninstaller @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
    }
    # Evidence and isolated data remain in ignored work/, never remove user data.
}
