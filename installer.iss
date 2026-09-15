#define MyAppName "TPS Bulk Video Editor"
#define MyAppVersion "1.3.1"
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
RestartApplications=no
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
AppMutex=TPSBulkVideoEditorAppMutex

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[InstallDelete]
Type: files; Name: "{app}\{#MyAppExeName}"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
