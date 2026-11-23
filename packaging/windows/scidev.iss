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

#ifdef CSharpPreview
  #define AppIdentity "{{8163BF31-5F43-4B08-B545-E6DDB95DA965}"
  #define ProductName "SciDevHarness C# Preview"
  #define ProgramFolder "SciDevHarness-CSharp-Preview"
  #define InstallerName "SciDevHarness-CSharp-Preview-Setup"
  #define DesktopExe "Workbench\SciDev.Desktop.exe"
  #define DesktopArgs ""
#else
  #define AppIdentity "{{8C6D7B1E-5A0F-4F9E-9F1A-6C2D8B7E4A31}"
  #define ProductName "SciDevHarness"
  #define ProgramFolder "SciDevHarness"
  #define InstallerName "SciDevHarness-Setup"
  #define DesktopExe "scidev_client.exe"
  #define DesktopArgs ""
#endif

[Setup]
AppId={#AppIdentity}
AppName={#ProductName}
AppVersion={#AppVersion}
AppVerName={#ProductName} {#AppVersion}
DefaultDirName={localappdata}\Programs\{#ProgramFolder}
DefaultGroupName={#ProductName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename={#InstallerName}-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#ProductName} {#AppVersion}
UninstallDisplayIcon={app}\{#DesktopExe}
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#ProductName}"; Filename: "{app}\{#DesktopExe}"; Parameters: "{#DesktopArgs}"
Name: "{autodesktop}\{#ProductName}"; Filename: "{app}\{#DesktopExe}"; Parameters: "{#DesktopArgs}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#DesktopExe}"; Parameters: "{#DesktopArgs}"; Description: "启动 {#ProductName}"; Flags: nowait postinstall skipifsilent
