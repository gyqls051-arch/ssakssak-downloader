; ============================================================
;  싹싹김치 다운로더 - canonical signed Inno Setup script
;  build_installer.bat만 공식 릴리스 진입점으로 사용한다.
;  내부 앱 EXE는 build\app에 생성되며 공개 배포하면 안 된다.
;  ※ 이 파일은 반드시 UTF-8(BOM) 인코딩으로 저장해야 한글이 안 깨짐.
; ============================================================

#ifndef MyAppVersion
  #error MyAppVersion must be supplied by build_installer.bat
#endif
#ifndef MyFileVersion
  #error MyFileVersion must be supplied by build_installer.bat
#endif

#ifndef PotBundleDir
#define PotBundleDir "potprovider"
#endif

#define MyAppName "싹싹김치 다운로더"
#define MyAppPublisher "싹싹김치"
#define MyAppURL "https://github.com/gyqls051-arch/ssakssak-downloader"
#define MyAppExeName "SsakSsakKimchiDownloader.exe"

[Setup]
; AppId 는 앱 고유 식별자. 절대 바꾸지 말 것 (바꾸면 업데이트가 아니라 별도 앱으로 설치됨)
AppId={{B4C1E7D2-5A83-4F6E-9C21-7E0D34A8F5B9}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases/latest
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 설치 프로그램
VersionInfoProductName={#MyAppName}
VersionInfoVersion={#MyFileVersion}
VersionInfoTextVersion={#MyAppVersion}
VersionInfoProductVersion={#MyFileVersion}
VersionInfoProductTextVersion={#MyAppVersion}
; 관리자 권한 불필요 - 사용자 폴더(AppData\Local\Programs)에 설치
PrivilegesRequired=lowest
DefaultDirName={autopf}\SsakSsakKimchiDownloader
DisableProgramGroupPage=yes
LicenseFile=docs\EULA.txt
OutputDir=dist
; updater와 GitHub release가 공유하는 유일한 공개 실행 자산 이름.
OutputBaseFilename=SsakSsakKimchiDownloader-Setup-v{#MyAppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; build_installer.bat이 --signtool=release=... 를 반드시 주입한다.
#ifndef UnsignedManualBuild
SignTool=release
SignedUninstaller=yes
SignedUninstallerDir=build\signed-uninstaller
#else
SignedUninstaller=no
#endif

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
; 바탕화면 아이콘 - 기본 체크됨
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "APP-ICON-LICENSE.txt"; DestDir: "{app}\docs"; Flags: ignoreversion
; 서명·버전 검증을 마친 내부 앱. 이 raw EXE 자체는 배포 금지.
Source: "build\app\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; 유튜브 PO Token 공급자 (Node.js 런타임 + 토큰 서버 + yt-dlp 플러그인).
Source: "{#PotBundleDir}\*"; DestDir: "{app}\potprovider"; Flags: ignoreversion recursesubdirs createallsubdirs
; yt-dlp는 hash-locked Python release environment에서 PyInstaller 앱 내부에 포함한다.
; writable external engine은 publisher provenance 경계를 우회하므로 설치하지 않는다.
; 사용자에게 제공해야 하는 정책·제3자 고지와 실제 release dependency inventory.
Source: "docs\licenses\*"; DestDir: "{app}\docs\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "docs\EULA.txt"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "docs\PRIVACY.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "docs\THIRD-PARTY-NOTICES.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "build\release-metadata\python-packages.json"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "build\release-metadata\requirements-release-win.lock"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "build\release-metadata\bundle-release-lock.json"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
; 앱 자동 업데이트는 /SILENT /AUTORUN=1로 설치한 뒤 새 버전을 다시 띄운다.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall; Check: WantsAutoRun

[Code]
// 앱 자동 업데이트로 실행됐는지(/AUTORUN=1) 판별.
function WantsAutoRun(): Boolean;
begin
  Result := ExpandConstant('{param:AUTORUN|0}') = '1';
end;
