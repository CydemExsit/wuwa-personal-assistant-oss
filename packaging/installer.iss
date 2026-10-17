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
Name: "chinesetraditional"; MessagesFile: "ChineseTraditional.isl"

[Files]
Source: "{#SourcePath}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#EditionName}"; Filename: "{app}\WuwaDisplayAssistant.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#EditionName}"; Filename: "{app}\WuwaDisplayAssistant.exe"; WorkingDir: "{app}"

[Run]
Filename: "{app}\WuwaDisplayAssistant.exe"; Description: "開啟 {#EditionName}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\WuwaDisplayAssistant.exe"; Parameters: "{code:UpdateArguments}"; Flags: nowait; Check: IsToolUpdate

[Code]
const
  Synchronize = $00100000;
  WaitObject0 = 0;
function OpenProcess(Access: LongWord; Inherit: Boolean; Pid: LongWord): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function GetLastError(): LongWord;
  external 'GetLastError@kernel32.dll stdcall';

function IsToolUpdate: Boolean;
begin
  Result := ExpandConstant('{param:WUWAUPDATE|0}') = '1';
end;

function UpdateArguments(Param: String): String;
begin
  Result := '--data-root ' + AddQuotes(ExpandConstant('{param:WUWADATA|}'));
  { Build verification uses the same restart entry with isolated data and no UI. }
  if ExpandConstant('{param:WUWASMOKETEST|0}') = '1' then
    Result := Result + ' --smoke-test';
end;

function InitializeSetup: Boolean;
var
  Parent: THandle;
  Pid: Integer;
begin
  Result := True;
  if not IsToolUpdate then Exit;
  Pid := StrToIntDef(ExpandConstant('{param:WUWAUPDATEPID|0}'), 0);
  if Pid <= 0 then begin Result := False; Exit; end;
  { Wait only for the exact assistant process that requested its own update. }
  Parent := OpenProcess(Synchronize, False, Pid);
  if Parent = 0 then begin
    Result := GetLastError() = 87; { Invalid PID: parent has already exited. }
    Exit;
  end;
  Result := WaitForSingleObject(Parent, 60000) = WaitObject0;
  CloseHandle(Parent);
  if not Result then
    MsgBox('工具尚未安全關閉，更新已停止。請關閉工具後重試。', mbError, MB_OK);
end;

{ User data lives outside the installation directory and survives update/uninstall. }
