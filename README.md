# 싹싹김치 비디오 다운로더

yt-dlp를 코어 엔진으로 사용하는 Windows 10/11용 영상 다운로더입니다. 유튜브·인스타그램·비메오·틱톡 등 yt-dlp가 지원하는 사이트에서 화질을 선택해 저장할 수 있습니다.

> **다운로드 권한이 있는 콘텐츠에만 사용하세요.** 저작권과 서비스 약관 준수 책임은 사용자에게 있습니다. DRM(기술적 보호조치) 우회 기능은 제공하지 않습니다.

## 사용자 설치

공식 배포 파일은 버전별 설치 프로그램입니다. 인증서가 없는 현재 수동 설치 빌드는 릴리스에 **미서명**으로 표시하며 SHA256SUMS.txt를 제공합니다. 서명 검증을 통과한 빌드만 앱 내부 자동 설치를 허용합니다.

- 현재 canonical 파일명: `SsakSsakKimchiDownloader-Setup-v1.10.exe`
- 다운로드: [GitHub Releases](https://github.com/gyqls051-arch/ssakssak-downloader/releases/latest)
- 환경: Windows 10/11, 설치형, 약 88MB(릴리스에 따라 달라질 수 있음)

공식 GitHub 릴리스의 파일명·SHA256SUMS.txt·미서명 여부를 대조하세요. 서명판이라고 안내된 파일의 서명이 없거나 유효하지 않은 경우 사용하지 말고 GitHub Issues에 신고하세요. 미서명 수동 설치판을 신뢰된 자동 업데이트로 처리하지 않습니다.

`build/app/SsakSsakKimchiDownloader.exe`와 `potprovider/`는 설치 프로그램을 만들기 위한 내부 산출물입니다. 이 파일·폴더를 개별 배포하면 필수 구성요소와 업데이트 신뢰 경계가 깨지므로 **배포 금지**입니다.

## 주요 구성

| 구성 | 역할 |
|---|---|
| yt-dlp | 사이트 분석과 다운로드 |
| ffmpeg | 영상·음성 병합, 변환, MP3 추출 |
| pywebview + WebView2 | Windows 데스크톱 UI |
| bgutil PO Token provider + Node.js | 유튜브 재생 토큰 보조 |

ffmpeg는 운영자가 독립 확인한 immutable release URL과 ZIP·추출 EXE 각각의 크기/SHA-256 pin이 모두 설정된 경우에만 사용자 데이터 폴더에 설치·실행합니다. yt-dlp는 검토한 Windows release lock에서 PyInstaller 앱 내부에 포함하며 writable 외부 엔진이나 런타임 PyPI 자동 업데이트를 사용하지 않습니다.

## 개인정보와 네트워크 동작

- 브라우저 쿠키 사용은 **기본 OFF**입니다. 사용자가 설정에서 명시적으로 켜고 로그인형 오류가 발생한 경우에만 로컬 브라우저 쿠키를 이용합니다.
- 다운로드 URL과 파일은 운영자 서버로 전송하지 않습니다. 대상 사이트와 GitHub의 앱·고정 ffmpeg 릴리스 등 기능 수행에 필요한 제3자 서버에는 직접 연결됩니다.
- 로그는 로컬에만 저장되며 URL의 사용자정보·경로·query·fragment는 제거됩니다.
- 안전한 request pinning이 마련되기 전까지 Playwright 브라우저 폴백은 소스 실행과 배포본 모두 비활성화되어 있습니다(`ENABLE_PLAYWRIGHT = False`).

자세한 내용: [개인정보 처리방침](docs/PRIVACY.md) · [최종 사용자 사용권 계약](docs/EULA.txt) · [제3자 소프트웨어 고지](docs/THIRD-PARTY-NOTICES.md)

## 데이터 위치

- 설정·로그·검증된 ffmpeg·업데이트 설치본 cache: `%LOCALAPPDATA%\SsakSsakKimchiDownloader\`
- 기본 저장 폴더: `내 다운로드\싹싹김치 다운로드\`
- 삭제하려면 앱 제거 후 위 사용자 데이터 폴더를 직접 삭제합니다.

## 개발 실행

```bash
python -m pip install -r requirements.txt
python main.py
```

`requirements.txt`는 개발용 범위 제약입니다. 상업 릴리스에는 Windows/Python 3.13에서 검토한 전체 전이 의존성을 exact version과 SHA-256으로 고정한 `requirements-release-win.lock`이 별도로 필요합니다. Node.js archive·그 안의 npm 버전·bgutil source는 `bundle-release-lock.json`의 SHA-256·exact version·immutable commit으로 고정되며, 빌드는 PATH의 Node/npm을 사용하지 않습니다.

## 상업 릴리스 빌드

공식 진입점은 `build_installer.bat` 하나입니다. 다음 환경변수를 설정한 Windows 빌드 머신에서 실행합니다.

```bat
set "SIGNTOOL_EXE=C:\Program Files (x86)\Windows Kits\10\bin\<sdk>\x64\signtool.exe"
set "SIGN_CERT_SHA1=<40-HEX-CERTIFICATE-THUMBPRINT>"
set "SIGN_TIMESTAMP_URL=https://<trusted-rfc3161-service>"
build_installer.bat
```

릴리스 게이트는 다음을 모두 강제합니다.

1. `main.py`, `version.json`, `version_info.txt`, 홈페이지의 버전 일치
2. `SIGN_CERT_SHA1`과 앱에 내장된 `TRUSTED_SIGNER_THUMBPRINTS` 일치
3. 해시가 포함된 exact transitive dependency lock과 확정된 법적 문서
4. 고정 Node/npm으로 새로 준비한 PO-token side bundle과 PyInstaller 앱 생성
5. 앱 EXE Authenticode 서명·timestamp·ProductVersion 검증
6. Inno Setup의 Setup 및 uninstaller 서명
7. `dist/SsakSsakKimchiDownloader-Setup-v1.10.exe`만 최종 실행 자산으로 존재하는지 재검증

현재 저장소는 실제 인증서 지문·법적 운영자/문의처·검토된 Windows lock과 immutable ffmpeg ZIP/EXE pin이 확정되기 전까지 의도적으로 release build가 실패합니다. 자세한 절차와 승인 조건은 [상업 릴리스 체크리스트](docs/COMMERCIAL-RELEASE-CHECKLIST.md)를 따르세요.

## 검증

```bash
python -m py_compile main.py prepare_bundle.py release_gate.py test_main.py
python -m pytest -q
```

새 릴리스는 추가로 서명된 Windows clean VM에서 설치·업데이트·제거를 종단 검증해야 합니다.
