; Installer Windows (Inno Setup 6). Non chiede permessi di amministratore:
; installa in %LOCALAPPDATA%\Programs\SaveSync.
#define AppVersion GetEnv("SAVESYNC_VERSION")
#if AppVersion == ""
  #define AppVersion "0.5.0"
#endif

[Setup]
AppId={{6F1C7E52-3B0B-4C4E-9B7A-5A3E2B1D9C11}
AppName=SaveSync
AppVersion={#AppVersion}
AppPublisher=SaveSync
DefaultDirName={localappdata}\Programs\SaveSync
DefaultGroupName=SaveSync
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=SaveSync-Setup-{#AppVersion}
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\SaveSync.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "it"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked
Name: "autostart"; Description: "Avvia SaveSync all'accensione del PC"

[Files]
Source: "dist\SaveSync\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\SaveSync"; Filename: "{app}\SaveSync.exe"
Name: "{userdesktop}\SaveSync"; Filename: "{app}\SaveSync.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "SaveSync"; ValueData: """{app}\SaveSync.exe"" --minimized"; \
  Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\SaveSync.exe"; Description: "Avvia SaveSync"; Flags: nowait postinstall skipifsilent
; aggiornamento automatico (installer silenzioso): riapre l'app a fine installazione
Filename: "{app}\SaveSync.exe"; Parameters: "--minimized"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM SaveSync.exe /F"; Flags: runhidden; RunOnceId: "KillApp"
