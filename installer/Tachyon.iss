; ==========================================================================
;  Tachyon Transcripts — Inno Setup Installer
; ==========================================================================
;
;  Builds an unsigned Windows installer that deposits the PyInstaller
;  one-folder bundle into %LocalAppData%\Programs\Tachyon Transcripts,
;  creates Start Menu + optional desktop shortcuts, and registers an
;  uninstaller under Add/Remove Programs.
;
;  Prerequisites before running this script:
;    1. Inno Setup 6.x compiler on PATH (iscc.exe)
;    2. PyInstaller has already produced dist\TachyonTranscripts\*
;       (see installer\tachyon.spec and installer\build_installer.bat)
;
;  Build:
;    iscc installer\Tachyon.iss
;
;  Output:
;    installer\dist\TachyonTranscripts-Setup-<version>.exe
;
;  The installer is NOT code-signed.  Windows SmartScreen and most
;  antivirus products will warn first-time users.  See docs/LEGAL.md.
; ==========================================================================

#define MyAppName        "Tachyon Transcripts"
#define MyAppShortName   "TachyonTranscripts"
#define MyAppVersion     "0.1.1"
#define MyAppPublisher   "PyroDS (Pyrodevstudio@gmail.com)"
#define MyAppURL         "https://github.com/PyroDS/projectTT"
#define MyAppExeName     "TachyonTranscripts.exe"
#define MyAppSourceDir   "..\dist\TachyonTranscripts"
#define MyAppIcon        "..\assets\icon.ico"

[Setup]
; NOTE: AppId is a random GUID and MUST NOT change between releases.
AppId={{7E0A2F3B-5B14-4A3E-9C2D-5A6E4F8D2A1C}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=no
LicenseFile=..\LICENSE
InfoBeforeFile=pre_install_notice.txt
OutputDir=dist
OutputBaseFilename={#MyAppShortName}-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; Install per-user (no admin required).  Because the installer is
; unsigned, forcing admin elevation would trigger an additional UAC
; + SmartScreen warning with no added benefit for a local app.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile={#MyAppIcon}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "startupicon"; Description: "&Start Tachyon Transcripts automatically when you sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; Main PyInstaller bundle (one-folder).  * with recursesubdirs picks up
; every DLL, .pyd, .pyz, and data file produced by the spec.
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Ship the legal disclaimer next to the exe so users can re-read it.
Source: "..\docs\LEGAL.md"; DestDir: "{app}\docs"; Flags: ignoreversion skipifsourcedoesntexist

; Ship the licence alongside.
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}";          Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}";     Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}";     Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
; Optional: pre-download the Whisper model so first launch isn't slow.
; The user can decline; the model will then be downloaded on first run.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--download-model"; \
    Description: "Pre-download the transcription model (~1 GB, ~3 minutes)"; \
    Flags: postinstall skipifsilent runasoriginaluser nowait

; Offer to launch the app after install — unchecked by default so the
; user can read the quick-start notes first.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; \
    Flags: postinstall skipifsilent nowait unchecked

[UninstallRun]
; Ensure the tray app is not still running so DLLs/log files under
; the one-folder bundle are not left behind due to file locks.
Filename: "{cmd}"; Parameters: "/C taskkill /IM ""{#MyAppExeName}"" /T /F >nul 2>&1"; \
    Flags: runhidden waituntilterminated; RunOnceId: "KillTachyonProcess"

[UninstallDelete]
; Purge per-user config + caches created under the install tree.
; User recordings in {app}\output are intentionally NOT removed.
; This may leave {app} behind if output data exists.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\assets"
Type: filesandordirs; Name: "{app}\docs"
Type: filesandordirs; Name: "{app}\models"
Type: files;          Name: "{app}\config.json"
Type: files;          Name: "{app}\tachyon.log"
Type: files;          Name: "{userdesktop}\{#MyAppName}.lnk"
Type: files;          Name: "{userstartup}\{#MyAppName}.lnk"
Type: filesandordirs; Name: "{group}"
Type: dirifempty;     Name: "{app}"

[Messages]
SetupAppTitle={#MyAppName} Setup
SetupWindowTitle={#MyAppName} Setup
