#define MyAppName "AI DocPrep"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "AI DocPrep Open Source"
#define MyAppExeName "AIDocPrep.exe"

[Setup]
; NOTE: The value of AppId uniquely identifies this application. Do not use the same AppId value in installers for other applications.
AppId={{9F82A1B7-4D3C-4E1A-8B6C-7D9E0F1A2B3C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}0
DisableProgramGroupPage=yes
OutputBaseFilename=AIDocPrep_Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile=icon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\AIDocPrep\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\AIDocPrep\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; NOTE: Don't use "Flags: ignoreversion" on any shared system files

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Automatically inject right-click "Convert to Markdown" context menus
; For Files
Root: HKCU; Subkey: "Software\Classes\*\shell\Convert to Markdown"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#MyAppExeName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\*\shell\Convert to Markdown\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletekey

; For Folders
Root: HKCU; Subkey: "Software\Classes\Directory\shell\Convert to Markdown"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#MyAppExeName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Directory\shell\Convert to Markdown\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletekey
