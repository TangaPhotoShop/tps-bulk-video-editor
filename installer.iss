#define MyAppName "TPS Bulk Video Editor"
#define MyAppVersion "1.3.3"
#define MyAppPublisher "Tangalooma Photo Shop"
#define MyAppExeName "TPS Bulk Video Editor.exe"

[Setup]
AppId={{85F6DFE9-E663-4FBB-9801-5B8BECD9B17E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\TPS Bulk Video Editor
DefaultGroupName={#MyAppName}
OutputDir=dist
OutputBaseFilename=TPS Bulk Video Editor Setup
SetupIconFile=branding\tps-video-editor-icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
CloseApplicationsFilter=TPS Bulk Video Editor.exe
RestartApplications=no
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
AppMutex=TPSBulkVideoEditorAppMutex

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[InstallDelete]
; Clear the previous registered installation before copying the replacement.
Type: filesandordirs; Name: "{app}\*"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
var
  ReplacementConfirmed: Boolean;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and
     FileExists(AddBackslash(WizardDirValue) + '{#MyAppExeName}') and
     (not ReplacementConfirmed) then
  begin
    Result := MsgBox(
      'An earlier TPS Bulk Video Editor installation was found.' + #13#10 + #13#10 +
      'Remove the previous version and install Version {#MyAppVersion}?',
      mbConfirmation, MB_YESNO) = IDYES;
    if Result then
      ReplacementConfirmed := True;
  end;
end;

procedure RemoveOldPortableCopies;
var
  UserProfile: String;
begin
  { Versions before 1.3 were portable downloads rather than registered installs. }
  UserProfile := GetEnv('USERPROFILE');
  if UserProfile <> '' then
  begin
    DeleteFile(AddBackslash(UserProfile) + 'Downloads\TPS Bulk Video Editor.exe');
    DeleteFile(AddBackslash(UserProfile) + 'Desktop\TPS Bulk Video Editor.exe');
  end;
  DeleteFile(ExpandConstant('{commondesktop}\TPS Bulk Video Editor.exe'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    RemoveOldPortableCopies;
end;
