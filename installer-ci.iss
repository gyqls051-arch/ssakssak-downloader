; ============================================================
;  싹싹김치 다운로더 - Inno Setup 설치 프로그램 스크립트
;  build.bat 로 dist\SsakSsakKimchiDownloader.exe 를 먼저 만든 뒤
;  build_installer.bat 실행 (또는 Inno Setup 에서 이 파일 컴파일).
;  ※ 이 파일은 반드시 UTF-8(BOM) 인코딩으로 저장해야 한글이 안 깨짐.
; ============================================================

; 새 버전 배포 시 여기 버전만 올리면 됨 (version.json 과 맞출 것)
#define MyAppName "싹싹김치 다운로더"
#define MyAppVersion "1.10"
#define MyAppPublisher "싹싹김치 패밀리"
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
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
; 관리자 권한 불필요 - 사용자 폴더(AppData\Local\Programs)에 설치
PrivilegesRequired=lowest
DefaultDirName={autopf}\SsakSsakKimchiDownloader
DisableProgramGroupPage=yes
OutputDir=dist
; 깃허브 릴리스는 첨부 파일명을 ASCII 로 강제 변환한다. 한글 이름을 쓰면
; '_._v1.8.exe' 처럼 뭉개져서 사용자가 받을 때 정상 파일로 안 보인다.
OutputBaseFilename=SsakSsakKimchiDownloader-Setup-v{#MyAppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
; 바탕화면 아이콘 - 기본 체크됨
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; 유튜브 PO Token 공급자 (Node.js 런타임 + 토큰 서버 + yt-dlp 플러그인).
; 유튜브가 SABR 을 강제해서 이게 없으면 유튜브 다운로드가 통째로 막힌다.
; exe 안에 넣지 않고 옆 폴더로 두는 이유: onefile 은 실행마다 압축을 풀어서
; 180MB 를 넣으면 시작이 크게 느려진다. build.bat 이 potprovider\ 를 만든다.
Source: "potprovider\*"; DestDir: "{app}\potprovider"; Flags: ignoreversion recursesubdirs createallsubdirs
; 다운로드 엔진(yt-dlp)은 exe 안에 번들된다 — 상업 배포본은 외부 엔진 폴더를
; 읽지 않는다(EXTERNAL_ENGINE_LOADING_ENABLED=False). 엔진 갱신은 앱 릴리스로
; 배포하므로 installer-ci 가 engine\ 를 따로 싣지 않는다.

[Icons]
; 시작 메뉴 + 바탕화면 바로가기를 한글 이름으로 생성
; → Windows 검색에서 "싹싹김치" 로 검색 가능해짐
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
; 앱이 스스로 업데이트할 때(main.py 의 AppUpdater 가 /SILENT /AUTORUN=1 로 실행)
; 설치가 끝나면 새 버전을 다시 띄운다. 위 항목은 skipifsilent 라 무인 설치에서는
; 실행되지 않으므로 이 줄이 따로 필요하다. Check 가 False 면 마법사에도 안 뜬다.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall; Check: WantsAutoRun

[Code]
// 앱 자동 업데이트로 실행됐는지(/AUTORUN=1) 판별.
function WantsAutoRun(): Boolean;
begin
  Result := ExpandConstant('{param:AUTORUN|0}') = '1';
end;
