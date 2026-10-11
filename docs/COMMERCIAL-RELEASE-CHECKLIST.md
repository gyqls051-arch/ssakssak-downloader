# 상업 릴리스 체크리스트

## 현재 상태: 배포 금지(Blocked)

다음 항목이 해결되기 전에는 홈페이지 게시, 고객 전달, 자동 업데이트용 업로드를 하면 안 됩니다.

- `main.py`의 `TRUSTED_SIGNER_THUMBPRINTS`가 비어 있음
- 실제 법적 운영자 명칭, 개인정보 문의처, EULA 효력 발생일·준거법이 미확정
- `requirements-release-win.lock`(Windows/Python 3.13 전체 전이 의존성 exact pin + SHA-256)이 없음
- immutable FFmpeg exact URL/tag, ZIP size/SHA-256, 추출 EXE size/SHA-256이 미확정
- 제3자 라이선스 원문·저작권 고지·FFmpeg GPL 의무 검토가 미완료
- Windows에서 SignTool/Inno/WebView2 설치·업데이트·제거 종단 검증을 수행하지 않음

기존 공개 v1.9 자산(`87592285` bytes, SHA-256 `a378262911ad46a9b56b0024cc13361274e2dae4b08b978d9c0a51b3e6971dd3`)은 서명자와 ProductVersion을 이 절차로 재검증하기 전까지 승인된 상업 릴리스로 간주하지 않습니다.

## 1. 법적·정책 승인

- [ ] 법적 운영자/배포자 명칭을 확정하고 `installer.iss`, `version_info.txt`, EULA, 개인정보 처리방침, 릴리스 페이지에 동일하게 기재
- [ ] 코드서명 인증서 Subject가 공식 게시자 안내와 일치하는지 법무/보안 담당자가 확인
- [ ] `docs/EULA.txt`의 모든 `RELEASE_BLOCKER_*` 값을 확정하고 법률 검토 승인
- [ ] `docs/PRIVACY.md`의 운영자·문의처·시행일을 확정하고 실제 코드의 데이터 흐름과 대조
- [ ] `docs/THIRD-PARTY-NOTICES.md`에 실제 lock/SBOM 기준 저작권·라이선스 원문·소스 제공 의무를 반영
- [ ] 유튜브 및 각 대상 서비스 약관, 저작권 안내, DRM 비지원 문구 검토

## 2. 공급망과 재현성

- [ ] 격리된 Windows 10/11, Python 3.13 환경에서 모든 직접·전이 Python 패키지를 exact version으로 결정
- [ ] 각 wheel/sdist의 SHA-256을 포함한 `requirements-release-win.lock` 생성
- [ ] lock은 `https://pypi.org/simple` 외 index, VCS/URL/editable requirement 없이 검토
- [ ] 안정판 yt-dlp 버전을 실제 유튜브 QA 후 선택(릴리스 빌드에서 nightly/latest 자동 설치 금지)
- [ ] `bundle-release-lock.json`의 Node.js ZIP SHA-256, 동봉 npm exact version, bgutil immutable commit을 공식 [Node SHASUMS256](https://nodejs.org/dist/v22.19.0/SHASUMS256.txt), archive의 npm package metadata 및 [GitHub tag ref](https://api.github.com/repos/Brainicism/bgutil-ytdlp-pot-provider/git/ref/tags/1.3.1)와 독립적으로 재대조
- [ ] bgutil `package-lock.json`의 integrity를 확인하고 검증한 archive의 exact `node.exe`+`npm-cli.js`로 설치·TypeScript 빌드·prune을 수행했는지, 배포 전 dev dependency와 build-only npm이 제거됐는지 확인
- [ ] BtbN FFmpeg의 immutable non-`latest` release URL/tag, ZIP size/SHA-256, 압축 내부 `ffmpeg.exe` size/SHA-256을 공식 원본과 독립 채널로 확인해 `main.py` pin을 모두 채움
- [ ] `build/release-metadata/python-packages.json`, lock, Git commit, 빌드 머신/SDK/Inno 버전을 릴리스 기록에 보존
- [ ] 취약점 스캔과 오탐 승인 기록 완료

## 3. 코드서명 준비

1. 코드서명 개인키는 OS 인증서 저장소/HSM에만 두고 `.pfx`, `.p12`, 비밀번호를 저장소에 커밋하지 않습니다.
2. 인증서 SHA-1 thumbprint(인증서 식별자)를 공백 없이 대문자로 `main.py`의 tuple에 넣습니다. 파일 서명 digest는 별도로 SHA-256입니다.
3. 인증서 교체 시 먼저 old+new thumbprint를 모두 신뢰하는 전환 릴리스를 기존 인증서로 배포하고, 보급 후 새 인증서로 서명합니다.
4. RFC 3161 HTTPS timestamp 서비스를 승인합니다.

```bat
set "SIGNTOOL_EXE=C:\Program Files (x86)\Windows Kits\10\bin\<sdk>\x64\signtool.exe"
set "SIGN_CERT_SHA1=<40-HEX-THUMBPRINT>"
set "SIGN_TIMESTAMP_URL=https://<approved-rfc3161-endpoint>"
```

`release_gate.py preflight`는 환경변수 누락, 잘못된 thumbprint, 앱 신뢰 목록 불일치, HTTP timestamp URL을 모두 차단합니다.

## 4. 버전과 문서 동기화

- [ ] `main.py` `APP_VERSION`
- [ ] `version.json` `latest_version`(단, 공개 게시 자체는 마지막 단계)
- [ ] `version_info.txt` file/product version(4부분)
- [ ] `web/index.html`의 tag, canonical Setup 링크·파일명
- [ ] README canonical 파일명
- [ ] 릴리스 노트와 지원 버전

`installer.iss`에는 버전을 하드코딩하지 않습니다. 검증된 값을 `build_installer.bat`이 `--define`으로 전달하며 누락 시 컴파일 오류가 나야 합니다.

## 5. Windows 릴리스 빌드

```bat
build_installer.bat
```

필수 순서는 다음과 같습니다.

1. 정적 계약·법적 placeholder·해시 lock·서명 환경 preflight
2. 깨끗한 venv에 `--require-hashes` 설치
3. `prepare_bundle.py --force`가 검증한 Node ZIP 안의 npm으로 PO-token bundle 생성(PATH Node/npm 금지)
4. PyInstaller 내부 앱 생성
5. 내부 앱 Authenticode 서명 후 Status/서명자/timestamp/ProductVersion 검증
6. Inno Setup `SignTool=release`, `SignedUninstaller=yes`로 Setup과 uninstaller 서명
7. 최종 Setup의 canonical 이름·서명자·timestamp·ProductVersion 재검증
8. `dist`의 실행 파일이 canonical Setup 하나뿐인지 확인

공식 결과는 `dist/SsakSsakKimchiDownloader-Setup-v{version}.exe` 하나입니다. `build/app/*.exe`와 `potprovider/`는 업로드 금지입니다. yt-dlp는 signed app 내부에만 존재하며 별도 writable `engine/`을 만들거나 설치하지 않습니다.

Inno 설정은 공식 [SignTool](https://jrsoftware.org/ishelp/topic_setup_signtool.htm), [SignedUninstaller](https://jrsoftware.org/ishelp/topic_setup_signeduninstaller.htm), [Compiler command line](https://jrsoftware.org/ishelp/topic_compilercmdline.htm) 규약을 따릅니다.

## 6. Clean VM 종단 QA

- [ ] Windows 10 최신 보안 업데이트, 일반 사용자 계정
- [ ] Windows 11 최신 보안 업데이트, 일반 사용자 계정
- [ ] Setup 파일 속성의 Authenticode `Valid`, 예상 Subject/thumbprint, timestamp 확인
- [ ] 설치 중 표시 게시자와 EULA 확인
- [ ] 설치된 앱 EXE와 `unins*.exe`도 동일 signer로 `Valid`
- [ ] 첫 실행, WebView2 UI, 일반 다운로드, 고화질 병합, MP3, 취소, 종료 보존
- [ ] 쿠키 기본 OFF 및 명시적 opt-in 경로
- [ ] ffmpeg 없는 상태의 검증 설치/실패 UX
- [ ] 구버전→신버전 자동 업데이트(사용자 클릭 필요)와 rollback/오프라인 UX
- [ ] 제거 후 프로그램 파일 제거, 사용자 데이터 보존/삭제 안내 확인
- [ ] SmartScreen 경고가 있더라도 우회 안내를 제공하지 않고 게시자 불일치 시 중단

## 7. GitHub 공개 순서

1. 서명된 Setup의 로컬 SHA-256·size·thumbprint·ProductVersion을 릴리스 기록에 남깁니다.
2. `v{version}` GitHub Release를 만들고 canonical Setup을 **유일한 실행 자산**으로 업로드합니다.
3. GitHub API의 asset 이름·size·`sha256:` digest를 로컬 값과 비교합니다.
4. 별도 clean VM에서 공개 URL로 다시 내려받아 서명과 hash를 재검증합니다.
5. 홈페이지 링크를 점검합니다.
6. **마지막에만** 원격 `version.json`을 새 버전으로 게시합니다. 순서를 뒤집으면 구버전 앱에 깨진/미검증 업데이트를 노출합니다.
7. 구버전 앱에서 업데이트 배너→사용자 클릭→다운로드→서명 검증→설치→재실행을 확인합니다.

## 8. 중단·회수 조건

서명 불일치, digest/size 불일치, ProductVersion 불일치, 예상하지 않은 추가 EXE, 법적 문서 불일치, clean VM 실패 중 하나라도 있으면 릴리스를 중단합니다. 이미 공개했다면 `version.json`을 새 버전으로 올리지 말고 자산을 회수한 뒤 새 버전 번호로 다시 빌드합니다. 서명되지 않은 원격 killswitch로 앱을 강제 종료하지 않습니다.
