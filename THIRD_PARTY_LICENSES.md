# 서드파티 라이선스

싹싹김치 비디오 다운로더의 **소스 코드**는 [MIT](LICENSE)입니다.
다만 빌드된 exe에는 아래 서드파티 컴포넌트가 함께 포함되며, 각 컴포넌트의
라이선스가 그대로 적용됩니다.

## exe에 번들되는 컴포넌트

| 컴포넌트 | 라이선스 | 용도 |
|---|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense (public domain) | 다운로드 코어 엔진 |
| [pywebview](https://github.com/r0x0r/pywebview) | BSD-3-Clause | 앱 UI (WebView2) |
| [curl_cffi](https://github.com/lexiforest/curl_cffi) | MIT | TLS 지문 임퍼소네이션 |
| [mutagen](https://github.com/quodlibet/mutagen) | **GPL-2.0-or-later** | 메타데이터/썸네일 임베딩 보조 |
| [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) | **GPL-3.0** | YouTube PO Token 공급자 |

## GPL 번들에 대한 안내

mutagen(GPL-2.0+)과 bgutil-pot-provider(GPL-3.0)는 PyInstaller exe 안에
포함되므로, **배포되는 exe 전체는 GPL 조건 하에 배포**됩니다.
이 저장소가 소스 전체를 공개하고 있으므로 소스 제공 의무는 충족됩니다.
(본 프로젝트의 고유 코드 자체는 MIT로 자유롭게 재사용 가능합니다.)

GPL 컴포넌트를 제외한 MIT-only 배포가 필요하면:
- `mutagen` 제거 → mp4/mp3 메타데이터는 ffmpeg 단독으로 처리됨 (기능 저하 있음)
- `bgutil-ytdlp-pot-provider` 제거 → YouTube SABR 스트림 다운로드 불가

## 실행 시 자동 다운로드 (exe에 미포함)

| 도구 | 라이선스 | 비고 |
|---|---|---|
| ffmpeg | LGPL/GPL (빌드에 따라 다름) | 첫 실행 시 `%LOCALAPPDATA%`에 자동 다운로드. 배포물에 미포함 |

## 소스 실행 전용 (배포 exe에 미포함)

| 도구 | 라이선스 |
|---|---|
| [Playwright](https://github.com/microsoft/playwright) | Apache-2.0 |
