# 제3자 소프트웨어 고지(배포 전 초안)

> **릴리스 차단:** `RELEASE_BLOCKER_THIRD_PARTY_REVIEW` — 실제 Windows release lock, side bundle, PyInstaller 결과를 기준으로 라이선스 담당자가 저작권 고지·라이선스 원문·소스 제공 의무를 확정하기 전에는 배포할 수 없습니다.

싹싹김치 비디오 다운로더는 여러 제3자 프로젝트를 사용합니다. 아래 목록은 검토 범위이며 최종 라이선스 목록이 아닙니다.

| 구성요소 | 사용 형태 | 확인할 원본 |
|---|---|---|
| Python 및 표준 라이브러리 | PyInstaller 애플리케이션 런타임 | Python 배포판 LICENSE/NOTICE |
| PyInstaller bootloader | 단일 앱 EXE 생성 | PyInstaller 라이선스 및 bootloader 예외 |
| yt-dlp | hash-locked release 빌드에서 앱 내부에 포함 | 실제 lock 버전의 저장소 LICENSE |
| pywebview, pythonnet, clr-loader | Windows WebView2 UI | 실제 lock 버전의 package metadata와 LICENSE |
| mutagen | 미디어 메타데이터 처리 | 실제 lock 버전 LICENSE |
| bgutil-ytdlp-pot-provider | Python plugin 및 Node.js 토큰 서버 | 고정 source commit의 LICENSE/NOTICE |
| Node.js와 번들된 구성요소 | 토큰 서버 런타임 | 해당 Node 배포판 LICENSE와 THIRD-PARTY-NOTICES |
| TypeScript/npm 전이 의존성 | 고정 Node archive를 사용한 토큰 서버 빌드; 배포 전 dev dependency 제거 | package-lock 기준 각 package 라이선스 |
| FFmpeg/BtbN GPL build | immutable archive·추출 EXE pin 검증 후 별도 실행 파일로 설치 | 정확한 FFmpeg release의 라이선스, configure 옵션, 대응 소스 제공 방식 |
| Inno Setup | 설치 프로그램 생성 | 사용한 Inno Setup 버전의 라이선스 |
| WebView2 Runtime | Windows 시스템/런타임 구성요소 | Microsoft 배포 조건 |

## 릴리스 전 필수 작업

1. `requirements-release-win.lock`과 `build/release-metadata/python-packages.json`의 모든 직접·전이 패키지를 대조합니다.
2. `potprovider/server/package-lock.json`의 모든 배포 runtime package와 Node.js 배포판의 동봉 고지를 수집합니다.
3. PyInstaller가 실제 포함한 DLL/native library까지 SBOM에 기록합니다.
4. 각 구성요소의 정확한 이름, 버전, 저작권자, SPDX 식별자, 프로젝트 URL을 확정합니다.
5. 재배포가 요구하는 라이선스 원문과 NOTICE를 설치 폴더 `docs/`에 포함합니다.
6. 사용 중인 BtbN FFmpeg가 GPL build이므로 해당 binary의 정확한 소스·빌드 설정 제공 또는 유효한 서면 제공 방식 등 GPL 의무를 법률 담당자가 승인해야 합니다.
7. copyleft 구성요소와 애플리케이션 결합 방식이 소스 제공 범위에 미치는 영향을 법률 검토합니다.
8. 취약점/라이선스 스캔 결과, 예외 승인, 공급 원본 SHA-256과 Git commit을 릴리스 기록에 보존합니다.

오픈소스 프로젝트의 이름과 상표는 각 소유자에게 귀속되며, 포함 사실이 본 제품에 대한 보증이나 후원을 의미하지 않습니다.
