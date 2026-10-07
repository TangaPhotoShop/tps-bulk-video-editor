#define MyAppName "TPS Photo Import & Backup"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "Tangalooma Photo Shop"
#define MyAppExeName "TPS Photo Import & Backup.exe"

[Setup]
AppId={{75EE6C93-128D-47BB-BFDF-6C4DFB4899A4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\TPS Photo Import & Backup
DefaultGroupName={#MyAppName}
OutputDir=dist
OutputBaseFilename=TPS.Photo.Import.Backup.Setup.v{#MyAppVersion}
SetupIconFile=assets\tps_photo_backup.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
CloseApplicationsFilter=TPS Photo Import & Backup.exe
RestartApplications=no
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
AppMutex=TPSPhotoImportBackupAppMutex

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\*"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent