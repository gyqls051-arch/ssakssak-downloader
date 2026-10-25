# 제3자 소프트웨어 고지

2026-10-24 Windows/Python 3.13 수동 배포 빌드. 실제 고정 의존성 및 원본 라이선스는 docs/licenses와 설치된 python-packages.json에 포함합니다. 각 제3자 구성요소에는 자체 라이선스가 우선 적용됩니다.

| 구성요소 | 버전 | 라이선스/고지 |
|---|---|---|
| altgraph | 0.17.5 | MIT |
| bgutil-ytdlp-pot-provider | 1.3.1 | 동봉 원본 LICENSE 참조 |
| bottle | 0.13.4 | MIT |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause; 원문 docs/licenses/cryptography |
| cffi | 2.1.1 | MIT-0 |
| clr-loader | 0.3.1 | 동봉 원본 LICENSE 참조 |
| mutagen | 1.47.0 | GPL-2.0-or-later |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pefile | 2024.8.26 | MIT |
| proxy-tools | 0.1.0 | MIT |
| pycparser | 3.12 | BSD-3-Clause |
| pyinstaller | 6.22.2 | GPLv2-or-later with a special exception which allows to use PyInstaller to build and distribute non-free programs (including commercial ones) |
| pyinstaller-hooks-contrib | 2026.9 | 동봉 원본 LICENSE 참조 |
| pythonnet | 3.2.1 | MIT |
| pywebview | 6.2.1 | BSD 3-Clause License |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause |
| setuptools | 84.2.1 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| wheel | 0.48.0 | MIT |
| yt-dlp | 2026.8.19 | Unlicense |

Python: PSF License. PyInstaller bootloader에는 상용·독점 프로그램 배포를 허용하는 예외가 있으며 동봉 원문을 확인하세요. Mutagen은 GPL-2.0-or-later로 배포하며 정확한 원본 버전의 소스 위치는 https://pypi.org/project/mutagen/1.47.0/#files 입니다. GPL 구성요소에는 본 앱의 프리웨어 재배포 제한을 적용하지 않습니다.

토큰 공급자: bundle-release-lock.json의 Node.js 22.19.0 및 bgutil 1.3.1 고정 커밋을 사용합니다. 런타임과 공급자의 자체 LICENSE/NOTICE는 potprovider 폴더에 보존됩니다. dev dependencies는 배포 번들에서 제외합니다.

FFmpeg: autobuild-2026-10-23-13-04 / ffmpeg-N-127957-g935f29eaae-win64-gpl.zip; 앱 EXE에 링크하거나 포함하지 않고, 사용 시 공식 BtbN 고정 자산에서 검증 후 별도 프로그램으로 실행합니다. 정확한 upstream/build/source 정보: https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-10-23-13-04 . FFmpeg 저작권·GPL 및 대응 소스 조건은 해당 배포 원문을 따릅니다.

UI WebView2: Microsoft Edge WebView2 Runtime을 사용하며 해당 런타임은 시스템 설치본을 이용합니다. 새 아이콘의 Lucide 원문 라이선스는 APP-ICON-LICENSE.txt에 있습니다.
