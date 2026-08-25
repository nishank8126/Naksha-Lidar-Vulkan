; ══════════════════════════════════════════════════════════════════
;  NakshaAI-LiDAR  -  Inno Setup Installer Script
;  Packages the PyInstaller dist folder into a single setup EXE
;  Build:  ISCC.exe naksha_setup.iss
; ══════════════════════════════════════════════════════════════════
#define MyAppName      "NakshaAI-LiDAR"
#define MyAppVersion   "2.0.4"
#define MyAppPublisher "NakshaAI"
; EXE name matches PyInstaller spec: name='NakshaAI-LiDAR'
#define MyAppExeName   "NakshaAI-LiDAR.exe"
#define MyAppIcon      "icons\naksha.ico"
#define DistDir        "dist\Naksha"
; ProgID must match register_snt_filetype.py: PROG_ID = "Naksha.SNTFile"
#define SntProgID      "Naksha.SNTFile"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=installer_output
OutputBaseFilename=NakshaAI-LiDAR_Setup
SetupIconFile={#MyAppIcon}
Compression=lzma2/fast
SolidCompression=no
DiskSpanning=no
PrivilegesRequired=admin
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; ── Main executable ──────────────────────────────────────────────
; NOTE: naksha.ico and snt_file.ico are bundled under _internal\gui\icons\
;       (from spec's gui/icons data entry). Do not reference _internal\icons\.
Source: "{#DistDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; ── Internal PyInstaller bundle (DLLs, .pyc, packages, icons, etc.)
; dist\Naksha\ only contains NakshaAI-LiDAR.exe + _internal\
; Everything (icons, data files, DLLs) lives inside _internal\
Source: "{#DistDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

; snt_core wheel is bundled by naksha.spec into _internal\wheels.
; This explicit entry makes the installer contract visible and also supports
; builds where the wheel is copied beside the .iss independently of PyInstaller.
Source: "snt_core-1.3.1-cp310-cp310-win_amd64.whl"; DestDir: "{app}\_internal\wheels"; Flags: ignoreversion skipifsourcedoesntexist

; ── Visual C++ 2015-2022 Redistributable (x64) ───────────────────
; OPTIONAL: Download VC_redist.x64.exe from https://aka.ms/vs/17/release/vc_redist.x64.exe
;           and place it in a redist\ subfolder next to this .iss file.
;           skipifsourcedoesntexist — build succeeds even without it.
Source: "redist\VC_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist

[Dirs]
; Pre-create the APPDATA log directory so the app always has a writable path on first launch.
Name: "{userappdata}\NakshaAI-LiDAR"

; ── Plugins directory (user-writable, avoids PermissionError in Program Files) ──
; PluginManager uses %LOCALAPPDATA%\NakshaAI-LiDAR\plugins in frozen builds
; to avoid WinError 5 "Access is denied" when creating/extracting plugins.
Name: "{localappdata}\NakshaAI-LiDAR\plugins"

[Icons]
Name: "{group}\{#MyAppName}";           Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\_internal\gui\icons\naksha.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";     Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; IconFilename: "{app}\_internal\gui\icons\naksha.ico"

[Registry]
; ── .snt file-type association ────────────────────────────────────
; ProgID matches register_snt_filetype.py (PROG_ID = "Naksha.SNTFile")
; and post_install.py (check_file_association_on_launch uses "Naksha.SNTFile").
; snt_file.ico path matches _get_icon_path() search order in register_snt_filetype.py:
;   exe_dir / "_internal" / "gui" / "icons" / "snt_file.ico"  <- what we set here
Root: HKCR; Subkey: ".snt";                                      ValueType: string; ValueName: ""; ValueData: "{#SntProgID}";          Flags: uninsdeletevalue
Root: HKCR; Subkey: "{#SntProgID}";                              ValueType: string; ValueName: ""; ValueData: "Naksha SNT File";         Flags: uninsdeletekey
Root: HKCR; Subkey: "{#SntProgID}\DefaultIcon";                  ValueType: string; ValueName: ""; ValueData: "{app}\_internal\gui\icons\snt_file.ico,0"
Root: HKCR; Subkey: "{#SntProgID}\shell\open\command";           ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""
; Content-Type for the extension (matches install_file_association in register_snt_filetype.py)
Root: HKCR; Subkey: ".snt";                                      ValueType: string; ValueName: "Content Type"; ValueData: "application/x-snt"; Flags: uninsdeletevalue

[Run]
; ── VC++ runtime (only runs if file was bundled) ──────────────────
Filename: "{tmp}\VC_redist.x64.exe"; Parameters: "/quiet /norestart"; StatusMsg: "Installing Visual C++ Runtime..."; Flags: waituntilterminated runhidden; Check: VCRedistFileExists

; ── Notify Windows Explorer to refresh icon cache after install ───
; SHChangeNotify is already called by register_snt_filetype._notify_shell()
; but running it via ie5.dll ensures icons refresh even if the app isn't launched yet.
Filename: "{sys}\ie4uinit.exe"; Parameters: "-show"; Flags: runhidden waituntilterminated skipifdoesntexist

; ── Launch app after install ──────────────────────────────────────
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; ── Remove .snt file association on uninstall ─────────────────────
; run the packaged EXE's lightweight maintenance command so HKCU keys are cleaned
; without depending on Python or a .py file association on the target machine
; (Inno Setup only removes HKCR keys from [Registry]; HKCU user-level keys
; written by check_file_association_on_launch would otherwise be orphaned).
Filename: "{app}\{#MyAppExeName}"; Parameters: "--unregister-snt"; Flags: runhidden waituntilterminated skipifdoesntexist

[Code]
function VCRedistFileExists(): Boolean;
begin
  Result := FileExists(ExpandConstant('{tmp}\VC_redist.x64.exe'));
end;
