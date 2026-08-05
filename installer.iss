; ============================================================
;  싹싹김치 다운로더 - Inno Setup 설치 프로그램 스크립트
;  build.bat 로 dist\SsakSsakKimchiDownloader.exe 를 먼저 만든 뒤
;  build_installer.bat 실행 (또는 Inno Setup 에서 이 파일 컴파일).
;  ※ 이 파일은 반드시 UTF-8(BOM) 인코딩으로 저장해야 한글이 안 깨짐.
; ============================================================

; 새 버전 배포 시 여기 버전만 올리면 됨 (version.json 과 맞출 것)
#define MyAppName "싹싹김치 다운로더"
#define MyAppVersion "1.7"
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
OutputBaseFilename=싹싹김치다운로더_설치_v{#MyAppVersion}
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

[Icons]
; 시작 메뉴 + 바탕화면 바로가기를 한글 이름으로 생성
; → Windows 검색에서 "싹싹김치" 로 검색 가능해짐
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
