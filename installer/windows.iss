#define AppName "모두의 회의록"
#define AppVersion "1.0.0"

[Setup]
AppId={{6C1E2B0A-6A54-4C55-9E2B-3F0D7B6D2A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=TheVR
AppPublisherURL=https://thevr.co.kr
DefaultDirName={localappdata}\Programs\ModuMeeting
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=..
OutputBaseFilename=ModuMeeting-windows-setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\ModuMeeting.exe
Compression=lzma2/ultra64
SolidCompression=yes
DiskSpanning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; Flags: checkedonce

[Files]
Source: "..\dist\ModuMeeting\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\ModuMeeting.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\ModuMeeting.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ModuMeeting.exe"; Description: "{#AppName} 실행"; Flags: nowait postinstall skipifsilent
