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
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView2

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\ModuMeeting.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\ModuMeeting.exe"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "화면 구성 요소(WebView2) 설치 중..."; Check: NeedsWebView2; Flags: waituntilterminated
Filename: "{app}\ModuMeeting.exe"; Description: "{#AppName} 실행"; Flags: nowait postinstall skipifsilent

[Code]
const WV2 = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasVersion(Root: Integer; Key: String): Boolean;
var V: String;
begin
  Result := RegQueryStringValue(Root, Key, 'pv', V) and (V <> '') and (V <> '0.0.0.0');
end;

function NeedsWebView2(): Boolean;
begin
  Result := not (HasVersion(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WV2) or
                 HasVersion(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WV2) or
                 HasVersion(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\' + WV2));
end;
