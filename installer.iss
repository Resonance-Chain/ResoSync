; Installer Windows (Inno Setup 6). Non chiede permessi di amministratore:
; installa in %LOCALAPPDATA%\Programs\ResoSync.
; Stesso AppId di quando si chiamava SaveSync: l'aggiornamento avviene sopra la vecchia
; installazione (nella sua cartella), senza doppioni nell'elenco dei programmi.
#define AppVersion GetEnv("RESOSYNC_VERSION")
#if AppVersion == ""
  #define AppVersion "0.5.9"
#endif

[Setup]
AppId={{6F1C7E52-3B0B-4C4E-9B7A-5A3E2B1D9C11}
AppName=ResoSync
AppVersion={#AppVersion}
AppPublisher=ResoSync
DefaultDirName={localappdata}\Programs\ResoSync
DefaultGroupName=ResoSync
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=ResoSync-Setup-{#AppVersion}
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\ResoSync.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "it"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked
Name: "autostart"; Description: "Avvia ResoSync all'accensione del PC"

[Files]
Source: "dist\ResoSync\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
; aggiornando da SaveSync: le opzioni di avvio di Steam e l'avvio automatico già impostati
; puntano a SaveSync.exe, quindi resta una copia con quel nome (stesso programma)
Source: "dist\ResoSync\ResoSync.exe"; DestDir: "{app}"; DestName: "SaveSync.exe"; \
  Flags: ignoreversion; Check: WasSaveSync

[InstallDelete]
; collegamenti col vecchio nome
Type: filesandordirs; Name: "{autoprograms}\SaveSync"
Type: files; Name: "{userdesktop}\SaveSync.lnk"

[Icons]
Name: "{group}\ResoSync"; Filename: "{app}\ResoSync.exe"
Name: "{userdesktop}\ResoSync"; Filename: "{app}\ResoSync.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "ResoSync"; ValueData: """{app}\ResoSync.exe"" --minimized"; \
  Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\ResoSync.exe"; Description: "Avvia ResoSync"; Flags: nowait postinstall skipifsilent
; aggiornamento automatico (installer silenzioso): riapre l'app a fine installazione
Filename: "{app}\ResoSync.exe"; Parameters: "--minimized"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM ResoSync.exe /F & taskkill /IM SaveSync.exe /F"; \
  Flags: runhidden; RunOnceId: "KillApp"

[UninstallDelete]
Type: files; Name: "{app}\SaveSync.exe"

[Code]
function WasSaveSync: Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\SaveSync.exe'));
end;
