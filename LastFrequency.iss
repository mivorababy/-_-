#define AppName "LAST FREQUENCY"
#define AppVersion "1.32"
#define AppExe "LastFrequency.exe"

[Setup]
AppId={{E6A3810D-5BB3-4C35-9AE7-417AEE1A1942}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=LAST FREQUENCY
DefaultDirName={localappdata}\Programs\Last Frequency
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
WizardStyle=modern
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=dist
OutputBaseFilename=LastFrequency-Setup
Compression=lzma2/ultra64
SolidCompression=yes
UsePreviousAppDir=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startmenu"; Description: "Создать ярлык в меню «Пуск»"; GroupDescription: "Создать ярлыки:"; Flags: checkedonce
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Создать ярлыки:"; Flags: unchecked

[InstallDelete]
; Старые установщики клали рядом с .exe копию папки web. Интерфейс теперь внутри .exe, лишняя папка удаляется.
Type: filesandordirs; Name: "{app}\web"

[Files]
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startmenu
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить {#AppName}"; Flags: postinstall nowait

[UninstallRun]
; Stop the player before removing its installed profile and any files it created beside the EXE.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM {#AppExe}"; Flags: runhidden waituntilterminated

[UninstallDelete]
; Player data (music, playlists, logs, settings, backgrounds) is intentionally stored beside the EXE.
; The user asked uninstall to remove the complete installation folder, including generated files.
Type: filesandordirs; Name: "{app}\*"
