; Inputs are supplied by scripts/build-installer.ps1 after package verification.
[Setup]
AppId={#EditionId}
AppName={#EditionName}
AppVersion={#AppVersion}
AppPublisher=CydemExsit
AppPublisherURL=https://github.com/{#Repository}
DefaultDirName={localappdata}\Programs\{#EditionId}
DefaultGroupName={#EditionName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
AllowNoIcons=yes
ShowLanguageDialog=no
OutputDir={#OutputDirectory}
OutputBaseFilename={#AssetName}-{#AppVersion}-Setup
Compression=lzma2/fast
SolidCompression=yes
SetupIconFile={#SourcePath}\_internal\wuwa_assistant\ui\styles\app.ico
UninstallDisplayIcon={app}\WuwaDisplayAssistant.exe
UninstallDisplayName={#EditionName}
CloseApplications=yes
RestartApplications=no
WizardStyle=modern

[Languages]
Name: "chinesetraditional"; MessagesFile: "compiler:Languages\ChineseTraditional.isl"

[Tasks]
Name: "desktopicon"; Description: "建立桌面捷徑"

[Files]
Source: "{#SourcePath}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#EditionName}"; Filename: "{app}\WuwaDisplayAssistant.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#EditionName}"; Filename: "{app}\WuwaDisplayAssistant.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\WuwaDisplayAssistant.exe"; Description: "開啟 {#EditionName}"; Flags: nowait postinstall skipifsilent

; Deliberately no data-directory or registry cleanup: application data and backups
; live outside {app}, and must survive update/uninstall.
