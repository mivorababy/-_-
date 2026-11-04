#define AppName "NOT soundLARP 100%"
#define AppVersion "1.32"
#define AppExe "NOT soundLARP 100%.exe"

[Setup]
AppId={{E6A3810D-5BB3-4C35-9AE7-417AEE1A1942}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
DefaultDirName={localappdata}\Programs\NOT soundLARP 100%
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
WizardStyle=modern
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=dist
OutputBaseFilename=NOT soundLARP 100%-Setup
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
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
; Rename the previous executable and shortcuts while preserving the installed profile.
Type: files; Name: "{app}\LastFrequency.exe"
Type: files; Name: "{autoprograms}\LAST FREQUENCY.lnk"
Type: files; Name: "{autodesktop}\LAST FREQUENCY.lnk"

[Files]
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startmenu
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить {#AppName}"; Flags: postinstall nowait

[UninstallRun]
; Stop the player before removing its installed profile and any files it created beside the EXE.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM ""{#AppExe}"""; Flags: runhidden waituntilterminated; RunOnceId: "StopNOTSoundLARP100"

[UninstallDelete]
; Player data (music, playlists, logs, settings, backgrounds) is intentionally stored beside the EXE.
; The user asked uninstall to remove the complete installation folder, including generated files.
Type: filesandordirs; Name: "{app}\*"
