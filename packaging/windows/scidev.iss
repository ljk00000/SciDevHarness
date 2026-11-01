; SciDevHarness per-user installer.
;
; Build it with scripts/build_installer.ps1. SourceDir, OutputDir and AppVersion
; are supplied by that script; no downloaded or generated payload is committed.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\deployment"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

[Setup]
AppId={{8C6D7B1E-5A0F-4F9E-9F1A-6C2D8B7E4A31}
AppName=SciDevHarness
AppVersion={#AppVersion}
AppVerName=SciDevHarness {#AppVersion}
DefaultDirName={localappdata}\Programs\SciDevHarness
DefaultGroupName=SciDevHarness
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=SciDevHarness-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=SciDevHarness {#AppVersion}
UninstallDisplayIcon={app}\scidev_client.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\SciDevHarness"; Filename: "{app}\scidev_client.exe"
Name: "{autodesktop}\SciDevHarness"; Filename: "{app}\scidev_client.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\scidev_client.exe"; Description: "启动 SciDevHarness"; Flags: nowait postinstall skipifsilent
