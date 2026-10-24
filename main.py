# -*- coding: utf-8 -*-
"""싹싹김치 비디오 다운로더 - 유튜브·인스타·비메오·틱톡 등 1800+ 사이트 지원 GUI

구조
----
- 상수/유틸    : 앱 메타데이터, 순수 함수
- 서비스 계층  : VideoAnalyzer / Downloader / UpdateChecker / FfmpegInstaller / Settings
                 (UI 비의존, 테스트 가능)
- UI 계층      : pywebview + web_ui/index.html (디자인·상태는 JS),
                 Api 클래스가 JS ↔ 파이썬 브리지 (window.pywebview.api)

사이드 이펙트는 서비스 계층과 Api 에 격리하고, UI(JS)는 콜백으로 결과만 받는다.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import http.client
import ipaddress
import io
import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ============================================================
#  다운로드 엔진(yt-dlp) 선택  ※ 반드시 `import yt_dlp` 보다 앞
# ============================================================
# 상업 배포본은 서명된 PyInstaller 앱 안에 requirements lock으로 포함된 yt-dlp만
# 실행한다. 설치 폴더나 LOCALAPPDATA의 Python 코드를 고르면 서명 경계를 우회하므로
# 과거 external engine 폴더는 로드하지 않는다.
_ENGINE_APP_ID = "SsakSsakKimchiDownloader"   # APP_ID 와 동일 (아래에서 검증)
ENGINE_VERSION_FILE = "engine_version.txt"
EXTERNAL_ENGINE_LOADING_ENABLED = False


def _engine_roots() -> list[Path]:
    """엔진 폴더 후보들. 존재 여부는 확인하지 않는다."""
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent / "engine")
    else:
        roots.append(Path(__file__).resolve().parent / "engine")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / _ENGINE_APP_ID / "engine")
    return roots


def _engine_version(d: Path) -> tuple[int, ...]:
    """엔진 폴더의 버전 표기를 비교 가능한 튜플로. 없거나 깨졌으면 빈 튜플."""
    try:
        raw = (d / ENGINE_VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ()
    out: list[int] = []
    for part in raw.split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


def _pick_engine() -> Path | None:
    """외부 Python 엔진은 서명 경계를 우회하므로 상업 배포에서 사용하지 않는다."""
    if not EXTERNAL_ENGINE_LOADING_ENABLED:
        return None
    best: Path | None = None
    best_v: tuple[int, ...] = ()
    for d in _engine_roots():
        try:
            if not (d / "yt_dlp" / "version.py").is_file():
                continue
        except OSError:
            continue
        v = _engine_version(d)
        if v > best_v:
            best, best_v = d, v
    return best


class _EngineFinder:
    """yt_dlp 임포트만 가로채 지정한 엔진 폴더에서 읽게 하는 메타 경로 파인더.

    PyInstaller 배포본에서는 sys.path 를 앞에 끼워 넣어도 소용이 없다
    (FrozenImporter 가 sys.meta_path 앞쪽에서 번들 모듈을 먼저 집어간다).
    그래서 meta_path 맨 앞에서 yt_dlp* 만 낚아채 엔진 폴더로 돌린다.
    엔진 폴더가 없으면 이 파인더를 아예 설치하지 않아 번들본이 그대로 쓰인다."""

    def __init__(self, root: Path):
        self._root = [str(root)]

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "yt_dlp" and not fullname.startswith("yt_dlp."):
            return None
        from importlib.machinery import PathFinder
        # 최상위는 엔진 폴더에서, 하위 모듈은 부모의 __path__(=엔진 폴더) 에서.
        return PathFinder.find_spec(
            fullname, self._root if fullname == "yt_dlp" else path, target)


def _activate_engine() -> str | None:
    root = _pick_engine()
    if root is None:
        return None
    sys.meta_path.insert(0, _EngineFinder(root))
    return str(root)


_ENGINE_PATH = _activate_engine()

import webview
import yt_dlp


# ============================================================
#  메타데이터 / 설정
# ============================================================

APP_TITLE = "싹싹김치 비디오 다운로더"
APP_VERSION = "1.10"
# APP_ID 는 로그/ffmpeg 캐시/config.json 폴더 경로(%LOCALAPPDATA%\<APP_ID>)에
# 쓰인다. 브랜드 식별자라 한 번 정하면 유지한다(바꾸면 기존 사용자 데이터 고아).
APP_ID = "SsakSsakKimchiDownloader"
# 엔진 로더는 import 순서상 APP_ID 보다 먼저 정의돼야 해서 문자열을 따로 갖는다.
# 둘이 어긋나면 갱신본을 못 찾으므로 여기서 못 박아 둔다.
assert _ENGINE_APP_ID == APP_ID, "엔진 로더의 APP_ID 가 어긋났습니다"

# HTTP User-Agent 는 반드시 ASCII 여야 한다(헤더는 latin-1). 한글 APP_TITLE 을
# 넣으면 UnicodeEncodeError 로 모든 요청이 실패하므로 ASCII APP_ID 를 쓴다.
HTTP_UA = f"{APP_ID}/{APP_VERSION}"

# 첫 실행 시 받을 이용약관/면책 동의 버전. 약관 본문은 web_ui/index.html 의
# '약관 모달'에 있다. 본문을 바꾸면 이 버전을 올려 재동의 받는다.
TOS_VERSION = "1"

# 제휴 광고 배너 링크 (배너 이미지는 web_ui/ 에 로컬 번들 — banner.png 등).
# UTM 파라미터로 소유관계 노출 없이 유입량만 측정할 수 있다.
PROMO_URL = "https://www.offcut.dev/?utm_source=ssakssak_downloader&utm_medium=app_banner"

# 업데이트 채널. 브랜드 격리를 위해 OFFCUT 과 무관한 별도(가능하면 별도 계정/조직)
# 공개 저장소에 version.json 을 올리고 아래 <your-account> 자리를 채우세요.
# <your-account> 가 남아 있으면 업데이트 체크를 건너뜁니다(=배포 전 안전 기본값).
VERSION_INFO_URL = "https://raw.githubusercontent.com/gyqls051-arch/ssakssak-downloader/main/version.json"
VERSION_CHECK_TIMEOUT = 3.0   # 초
VERSION_JSON_MAX_BYTES = 10_000  # 응답 크기 제한 (메모리 보호)
# 설치본(.exe)이 올라가는 릴리스 저장소. 앱이 스스로 새 버전을 받을 때
# 이 저장소의 릴리스 API 에서 v<버전> 태그의 에셋을 찾는다(AppUpdater).
# version.json 과 같은 저장소여야 한다.
GITHUB_RELEASE_REPO = "gyqls051-arch/ssakssak-downloader"
CANONICAL_SETUP_NAME_FMT = "SsakSsakKimchiDownloader-Setup-v{version}.exe"
TRUSTED_UPDATE_HOSTS = frozenset({
    "github.com", "api.github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})
# FFmpeg는 배포 승인 때 immutable release tag/URL과 archive·추출 EXE의
# size/SHA-256을 독립적으로 확인해 모두 채운다. 하나라도 비어 있으면 기존 cache를
# 포함해 어떤 ffmpeg.exe도 실행하지 않는다. mutable "latest"는 허용하지 않는다.
FFMPEG_RELEASE_TAG = "autobuild-2026-10-23-13-04"
FFMPEG_ASSET_NAME = "ffmpeg-N-127957-g935f29eaae-win64-gpl.zip"
FFMPEG_ARCHIVE_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-23-13-04/ffmpeg-N-127957-g935f29eaae-win64-gpl.zip"
FFMPEG_ARCHIVE_SIZE = 200466434
FFMPEG_ARCHIVE_SHA256 = "fcf14fbbf9937ead11f464c6ecc73bc30400441ab6048c8cf3c55d77fd1c1042"
FFMPEG_EXECUTABLE_SIZE = 169039360
FFMPEG_EXECUTABLE_SHA256 = "6a7f66957267ebe36c087036fa5e3e507c05c3b666416f3b5c2c8ba5de15bf96"
# 상업 릴리스 코드서명 인증서의 SHA-1 thumbprint를 대문자·공백 없이 넣는다.
# 비어 있으면 자동 설치는 fail-closed로 중단된다. 인증서 발급 후 앱/Setup을 모두
# 같은 인증서로 서명하고 이 목록을 릴리스 빌드에서 반드시 채워야 한다.
TRUSTED_SIGNER_THUMBPRINTS: tuple[str, ...] = ()
# 원격 version.json은 아직 독립 서명되지 않았으므로 killswitch는 신뢰하지 않는다.
REMOTE_KILLSWITCH_ENABLED = False
# 모든 yt-dlp 요청은 urllib 단일 handler, public-IP socket pinning, redirect 검사를
# 거친다. 이 경계를 만들 수 없는 curl_cffi/WebSocket/external downloader는 거부한다.
SECURE_YTDLP_TRANSPORT_ENABLED = True

# 화질 라벨 → 높이(px). None=최고화질 자동, -1=음성만
QUALITY_TABLE: list[tuple[str, int | None]] = [
    ("최고화질 (자동)", None),
    ("4320p (8K)",     4320),
    ("2160p (4K)",     2160),
    ("1440p (2K)",     1440),
    ("1080p (FHD)",    1080),
    ("720p (HD)",       720),
    ("480p",            480),
    ("360p",            360),
    ("음성만 (MP3)",    -1),
]

# 출력 포맷 라벨 → 컨테이너
FORMAT_TABLE: list[tuple[str, str]] = [
    ("MP4 (호환성 우선)",    "mp4"),
    ("MKV (최고화질·무손실)", "mkv"),
]

# 일반 동영상 링크 인식 (http/https). yt-dlp 가 지원하는 1800+ 사이트를 모두
# 시도할 수 있도록, 유튜브 전용이 아니라 모든 http(s) URL 을 받아들인다.
URL_RE = re.compile(r"^https?://[^\s]+", re.IGNORECASE)
MAX_URL_LENGTH = 4096
JOB_ID_RE = re.compile(r"^d[a-f0-9-]{16,64}$", re.IGNORECASE)
MAX_ACTIVE_DOWNLOADS = 3
ANALYSIS_TTL_SECONDS = 10 * 60
MAX_ANALYSIS_CONTEXTS = 32
SHUTDOWN_TIMEOUT_SECONDS = 8.0
COOKIE_BROWSERS = frozenset({
    "chrome", "edge", "whale", "brave", "firefox", "vivaldi", "opera",
})
MEDIA_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".opus", ".ogg",
    ".flac", ".wav", ".aac", ".mov", ".avi", ".ts", ".m2ts",
})

# ── 성공률 보강용 상수 ──────────────────────────────────────
# 일부 사이트는 우리 앱 UA(HTTP_UA)나 yt-dlp 기본 UA 를 막는다. 다운로드가
# 실패하면 흔한 데스크톱 브라우저 UA + Referer/Origin 헤더로 한 번 더 시도한다.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 멈춘 연결에 무한 대기하지 않게 하는 네트워크 타임아웃(초).
SOCKET_TIMEOUT = 30

# ── 유튜브 PO Token 공급자 (SABR 대응) ─────────────────────
# 유튜브가 SABR 스트리밍을 강제하면서, PO Token 없이는 어떤 재생 클라이언트로도
# 실제 스트림을 받을 수 없다(포맷 목록이 비거나, 받아도 10MB 즈음 403 Forbidden).
# bgutil POT 공급자를 로컬 HTTP 서버로 띄우고 yt-dlp 플러그인이 토큰을 받아온다.
# 번들 구성: potprovider/{node.exe, server/build/main.js, server/node_modules, plugin/}
POT_PORT = 4416
POT_BASE_URL = f"http://127.0.0.1:{POT_PORT}"
POT_STARTUP_TIMEOUT = 25.0        # 서버 기동 대기 상한(초)

# 유튜브 재생 클라이언트 재시도 순서. None = yt-dlp 기본 선택(화질 목록이 가장
# 풍부하지만, 종종 스트림이 10MB 즈음 403 으로 끊긴다). 그다음부터는 실제
# 다운로드가 끝까지 되는 걸 확인한 클라이언트들로 내려간다.
# 어떤 게 살아 있는지는 유튜브가 수시로 바꾸므로 '하나 고정'이 아니라
# 순서대로 시도하는 사다리로 둔다.
YOUTUBE_CLIENT_LADDER: list[str | None] = [None, "tv_simply", "android", "tv", "web"]

# DRM(기술적 보호조치) 보호 콘텐츠로 보이는 오류 키워드. 감지되면 우회를
# 절대 시도하지 않고(불법) 즉시 명확히 안내한 뒤 중단한다.
DRM_HINTS = ("drm", "widevine", "playready", "fairplay")

# '받는 데'가 아니라 '저장하는 데'서 실패했음을 알리는 오류 키워드.
# (같은 이름의 파일이 재생기에 열려 있으면 마지막 이름 바꾸기가 WinError 5 로
#  죽는다. 예전엔 이걸 네트워크 실패로 착각해 방식을 10번 바꿔가며 재시도했고,
#  마지막 시도의 엉뚱한 메시지가 화면에 떴다.)
OUTPUT_ERROR_HINTS = (
    "액세스가 거부",             # WinError 5 (한국어 윈도우)
    "access is denied",
    "winerror 5",
    "winerror 32",
    "다른 프로세스가 사용",       # WinError 32
    "used by another process",
    "permission denied",
    "no space left",
    "디스크 공간",
)

# 2단계 폴백: yt-dlp 가 사이트를 인식 못 할 때, 페이지 HTML 에서 직접 찾을
# 스트리밍 매니페스트(HLS .m3u8 / DASH .mpd) 패턴.
MANIFEST_RE = re.compile(
    r"""https?://[^\s"'<>\\]+?\.(?:m3u8|mpd)(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)

# Playwright는 브라우저 내부의 모든 하위 요청/redirect를 검증된 IP에 고정하기
# 전에는 private-network 요청을 완전히 막을 수 없다. 상업 배포와 소스 실행 모두
# 안전 우선으로 비활성화한다.
ENABLE_PLAYWRIGHT = False


# ============================================================
#  로깅
# ============================================================

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def safe_url_for_log(value: object) -> str:
    """URL의 host만 남기고 credential/path/query/fragment를 로그에서 제거한다."""
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
        host = parsed.hostname or ""
        if not host or parsed.scheme.lower() not in ("http", "https"):
            return "<redacted-url>"
        display_host = f"[{host}]" if ":" in host else host
        port = parsed.port
        suffix = f":{port}" if port is not None else ""
        return f"{parsed.scheme.lower()}://{display_host}{suffix}/…"
    except (TypeError, ValueError):
        return "<redacted-url>"


def redact_urls_in_text(value: object) -> str:
    """외부 라이브러리 예외/로그 안 URL에서 비밀 query와 userinfo를 제거한다."""
    text = str(value or "")
    return _URL_IN_TEXT_RE.sub(lambda match: safe_url_for_log(match.group(0)), text)


class _RedactingLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_urls_in_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_urls_in_text(v) if isinstance(v, str) else v
                                for v in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_urls_in_text(v) if isinstance(v, str) else v
                for key, v in record.args.items()
            }
        return True


class _RedactingFormatter(logging.Formatter):
    def formatException(self, exc_info) -> str:
        return redact_urls_in_text(super().formatException(exc_info))


def setup_logging() -> Path:
    """파일 로그 설정. URL credential/query/fragment는 저장 전에 제거한다."""
    log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_ID
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=512_000, backupCount=2, encoding="utf-8"
    )
    handler.addFilter(_RedactingLogFilter())
    handler.setFormatter(
        _RedactingFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    return log_file


log = logging.getLogger("ssakssak")


# ============================================================
#  순수 유틸
# ============================================================

def build_format_selector(container: str) -> str:
    """컨테이너에 맞춘 yt-dlp 포맷 셀렉터.

    해상도 선호는 여기서 필터링하지 않는다 — 'height<=N' 필터는 세로 영상
    (Shorts 1080×1920)에서 실제 FHD 스트림을 걸러낸 뒤 480p를 골라버리는
    버그가 있었다. 화질은 format_sort(res:N)가 '짧은 변' 기준으로 랭킹한다.
    MP4 선택 시 H.264+AAC만 골라야 재인코딩 없이 무손실로 담긴다."""
    if container == "mp4":
        return (
            "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "best[ext=mp4]/best"
        )
    return "bestvideo+bestaudio/best"


def build_format_sort(height: int | None) -> list[str]:
    """선택 화질의 format_sort 표현.

    yt-dlp 의 res 는 min(width,height) 즉 '짧은 변' 이라 가로든 세로든
    사람이 말하는 화질(1080p)과 같다. res:N 은 N 이하를 큰 것부터 먼저,
    없으면 N 초과 중 가장 작은 것을 고른다 — 조용한 저해상도 폴백 대신
    가장 가까운 실제 화질을 받는다."""
    return [f"res:{height}"] if isinstance(height, int) and height > 0 else []


def parse_version(v: str) -> tuple[int, ...]:
    """'1.2.3' → (1,2,3). 잘못된 토큰은 0으로 떨어뜨려 안전."""
    parts: list[int] = []
    for p in (v or "0").strip().split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def compare_versions(a: str, b: str) -> int:
    """버전 문자열 a, b 비교. a<b → -1, a==b → 0, a>b → 1.

    두 튜플을 동일 길이로 0 패딩한 뒤 비교하므로
    '1.0' 과 '1.0.0' 은 같은 버전으로 취급된다(오판 제거)."""
    ta, tb = parse_version(a), parse_version(b)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


def is_safe_http_url(url: str | None) -> bool:
    """외부 브라우저용 URL의 기본 문법 검사. 네트워크 출처 검사는 별도 수행."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        return False
    if any(ord(ch) < 32 or ch.isspace() for ch in url):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        _ = parsed.port
    except ValueError:
        return False
    return (parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None)


def _require_global_ip(address: str) -> None:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as exc:
        raise RuntimeError("주소의 IP를 확인할 수 없습니다") from exc
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        raise RuntimeError("내부·로컬 네트워크 주소는 사용할 수 없습니다")


def validate_public_http_url(url: object, *, require_https: bool = False,
                             resolve_dns: bool = True) -> str:
    """사용자/manifest URL이 public HTTP(S) 목적지만 가리키는지 검증한다."""
    if not isinstance(url, str) or not is_safe_http_url(url):
        raise RuntimeError("올바른 공개 링크(http/https)가 아닙니다")
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    if require_https and scheme != "https":
        raise RuntimeError("HTTPS 공개 링크만 사용할 수 있습니다")
    host = (parsed.hostname or "").rstrip(".").lower()
    if (not host or host == "localhost" or host.endswith(".localhost")
            or host.endswith(".local") or host.endswith(".internal")):
        raise RuntimeError("내부·로컬 네트워크 주소는 사용할 수 없습니다")
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        _require_global_ip(str(literal))
    elif resolve_dns:
        try:
            infos = socket.getaddrinfo(
                host, parsed.port or (443 if scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError("링크의 서버 주소를 확인할 수 없습니다") from exc
        addresses = {str(info[4][0]) for info in infos if info and info[4]}
        if not addresses:
            raise RuntimeError("링크의 서버 주소를 확인할 수 없습니다")
        for address in addresses:
            _require_global_ip(address)
    return url


def _is_owned_pot_url(url: object) -> bool:
    """앱이 직접 띄운 PO-token 서버의 정확한 두 endpoint만 식별한다."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "http"
        and parsed.hostname == "127.0.0.1"
        and port == POT_PORT
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path in ("/ping", "/get_pot")
    )


def _validate_ytdlp_request_url(url: object, *, allow_owned_pot: bool) -> str:
    if allow_owned_pot and _is_owned_pot_url(url):
        return str(url)
    return validate_public_http_url(url)


def _public_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                              source_address=None):
    """검증한 getaddrinfo 결과의 sockaddr에 직접 연결해 DNS rebinding을 막는다."""
    try:
        host, port = address
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise OSError("invalid network address") from exc
    host_text = str(host).rstrip(".")
    owned_pot = host_text == "127.0.0.1" and port == POT_PORT
    try:
        infos = socket.getaddrinfo(host_text, port, 0, socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise OSError("network address resolution failed") from exc
    if not infos:
        raise OSError("network address resolution returned no results")

    approved = []
    for info in infos:
        address_text = str(info[4][0])
        if owned_pot and address_text == "127.0.0.1":
            approved.append(info)
            continue
        try:
            _require_global_ip(address_text)
        except RuntimeError as exc:
            raise OSError("private or non-global network destination blocked") from exc
        approved.append(info)

    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in approved:
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    if last_error is not None:
        raise last_error
    raise OSError("no approved network address was connectable")


def _pinned_connection_factory(http_class, source_address, *args, **kwargs):
    connection = http_class(*args, **kwargs)
    connection._create_connection = _public_create_connection
    if source_address is not None:
        connection.source_address = (
            source_address if isinstance(source_address, tuple)
            else (source_address, 0)
        )
    return connection


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        factory = functools.partial(
            _pinned_connection_factory, http.client.HTTPConnection, None)
        return self.do_open(factory, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        factory = functools.partial(
            _pinned_connection_factory, http.client.HTTPSConnection, None)
        kwargs = {"context": self._context}
        if hasattr(self, "_check_hostname"):
            kwargs["check_hostname"] = self._check_hostname
        return self.do_open(factory, req, **kwargs)


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    """redirect Location을 연결 전에 검사해 private-network 이동을 차단한다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute = urllib.parse.urljoin(req.full_url, newurl)
        validate_public_http_url(absolute)
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def _open_public_url(request: urllib.request.Request, timeout: float):
    validate_public_http_url(request.full_url)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _PinnedHTTPHandler(),
        _PinnedHTTPSHandler(),
        _PublicRedirectHandler(),
    )
    response = opener.open(request, timeout=timeout)
    try:
        validate_public_http_url(response.geturl())
    except Exception:
        response.close()
        raise
    return response


_SECURE_YTDL_CLASS: type | None = None
_SECURE_YTDL_BASE: object | None = None


def _secure_ytdl_class():
    """urllib 단일 transport와 pinned connector를 강제한 YoutubeDL subclass."""
    global _SECURE_YTDL_CLASS, _SECURE_YTDL_BASE
    base = yt_dlp.YoutubeDL
    # test_main의 최소 stub은 networking package가 없다. 실제 yt-dlp package에서
    # import가 실패하면 조용히 약한 transport로 돌아가지 않고 즉시 중단한다.
    if not hasattr(yt_dlp, "__path__"):
        return base
    if _SECURE_YTDL_CLASS is not None and _SECURE_YTDL_BASE is base:
        return _SECURE_YTDL_CLASS
    try:
        from yt_dlp.downloader import (DashSegmentsFD, HlsFD, HttpFD,
                                       get_suitable_downloader)
        from yt_dlp.networking import _urllib as ytdlp_urllib
        from yt_dlp.networking.exceptions import UnsupportedRequest
    except ImportError as exc:
        raise RuntimeError("안전한 yt-dlp 네트워크 transport를 구성할 수 없습니다") from exc

    class PinnedYtdlpHTTPHandler(ytdlp_urllib.HTTPHandler):
        def http_open(self, req):
            factory = functools.partial(
                _pinned_connection_factory, http.client.HTTPConnection,
                self._source_address)
            return self.do_open(factory, req)

        def https_open(self, req):
            factory = functools.partial(
                _pinned_connection_factory, http.client.HTTPSConnection,
                self._source_address)
            return self.do_open(factory, req, context=self._context)

    class PublicYtdlpRedirectHandler(ytdlp_urllib.RedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            absolute = urllib.parse.urljoin(req.full_url, newurl)
            # 원격 응답의 redirect는 앱 소유 localhost endpoint도 절대 사용할 수 없다.
            _validate_ytdlp_request_url(absolute, allow_owned_pot=False)
            return super().redirect_request(req, fp, code, msg, headers, absolute)

    class PublicUrllibRH(ytdlp_urllib.UrllibRH):
        _SUPPORTED_URL_SCHEMES = ("http", "https")
        _SUPPORTED_PROXY_SCHEMES = ()
        _SUPPORTED_FEATURES = ()

        def _check_proxies(self, proxies):
            if proxies and any(value for value in proxies.values()):
                raise UnsupportedRequest("Proxies are disabled by the public-network policy")

        def _create_instance(self, proxies, cookiejar, legacy_ssl_support=None):
            opener = urllib.request.OpenerDirector()
            handlers = [
                PinnedYtdlpHTTPHandler(
                    debuglevel=int(bool(self.verbose)),
                    context=self._make_sslcontext(
                        legacy_ssl_support=legacy_ssl_support),
                    source_address=self.source_address,
                ),
                ytdlp_urllib.HTTPCookieProcessor(cookiejar),
                ytdlp_urllib.UnknownHandler(),
                ytdlp_urllib.HTTPDefaultErrorHandler(),
                ytdlp_urllib.HTTPErrorProcessor(),
                PublicYtdlpRedirectHandler(),
            ]
            for handler in handlers:
                opener.add_handler(handler)
            opener.addheaders = []
            return opener

        def _send(self, request):
            _validate_ytdlp_request_url(request.url, allow_owned_pot=True)
            response = super()._send(request)
            try:
                _validate_ytdlp_request_url(response.url, allow_owned_pot=True)
            except Exception:
                response.close()
                raise
            return response

    allowed_downloaders = (HttpFD, HlsFD, DashSegmentsFD)

    class SecureYoutubeDL(base):
        def build_request_director(self, handlers, preferences=None):
            return super().build_request_director((PublicUrllibRH,), preferences)

        def urlopen(self, req):
            url = req if isinstance(req, str) else (
                getattr(req, "url", None) or getattr(req, "full_url", None))
            _validate_ytdlp_request_url(url, allow_owned_pot=True)
            return super().urlopen(req)

        def process_info(self, info_dict):
            candidates = info_dict.get("requested_formats") or [info_dict]
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    raise yt_dlp.utils.DownloadError(
                        "검증할 수 없는 다운로드 형식이 차단되었습니다")
                downloader = get_suitable_downloader(
                    dict(candidate), self.params)
                if downloader not in allowed_downloaders:
                    raise yt_dlp.utils.DownloadError(
                        "외부 네트워크 downloader가 필요한 형식은 안전상 지원하지 않습니다")
            return super().process_info(info_dict)

    _SECURE_YTDL_CLASS = SecureYoutubeDL
    _SECURE_YTDL_BASE = base
    return SecureYoutubeDL


def _guarded_youtube_dl(opts: dict):
    if not SECURE_YTDLP_TRANSPORT_ENABLED:
        raise RuntimeError("안전한 yt-dlp 네트워크 transport가 비활성화되었습니다")
    secured = dict(opts)
    secured.update({
        "proxy": "",
        "external_downloader": {"default": "native"},
        "hls_prefer_native": True,
    })
    return _secure_ytdl_class()(secured)


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_RELEASE_VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value.strip()) is not None


def _normalize_release_version(value: object) -> str:
    """앱 릴리스 버전을 canonical 파일명/PE 버전 비교용으로 정규화한다."""
    version = str(value or "").strip()
    if version.lower().startswith("v"):
        version = version[1:]
    if _RELEASE_VERSION_RE.fullmatch(version) is None:
        raise RuntimeError("업데이트 버전 형식이 올바르지 않습니다")
    return version


def _require_trusted_https_url(url: object, hosts: frozenset[str],
                               label: str = "다운로드 주소") -> str:
    """실행 코드/엔진을 받을 URL을 HTTPS exact-host allowlist로 제한한다."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise RuntimeError(f"{label}가 올바르지 않습니다")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"{label}가 올바르지 않습니다") from exc
    host = (parsed.hostname or "").lower()
    if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
            or parsed.password is not None or (port is not None and port != 443)
            or host not in hosts):
        raise RuntimeError(f"{label}의 출처를 신뢰할 수 없습니다")
    return url


def _require_trusted_response_url(response, requested_url: str,
                                  hosts: frozenset[str],
                                  label: str = "리디렉션 주소") -> None:
    """urllib 자동 redirect 뒤 최종 URL도 동일한 allowlist인지 확인한다."""
    getter = getattr(response, "geturl", None)
    final_url = getter() if callable(getter) else requested_url
    _require_trusted_https_url(final_url or requested_url, hosts, label)


def safe_open_url(url: str | None) -> bool:
    try:
        public_url = validate_public_http_url(url)
    except RuntimeError:
        log.warning("opening unsafe url blocked: %s", safe_url_for_log(url))
        return False
    webbrowser.open(public_url)
    return True


def ffmpeg_cache_dir() -> Path:
    """자동 설치된 ffmpeg.exe 가 저장되는 위치."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_ID / "ffmpeg" / "bin"


def ytdlp_cache_dir() -> str:
    """yt-dlp 플레이어 JS/서명 캐시 위치. 분석(extract_info)이 매번 플레이어 JS 를
    다시 받지 않도록 영속 캐시를 둬 반복 분석을 조금 더 빠르게 한다."""
    return str(Path(os.environ.get("LOCALAPPDATA") or Path.home())
               / APP_ID / "ytdlp-cache")


# 저장 폴더가 앱 폴더와 다른 드라이브일 때, 저장 폴더 안에 만드는 작업 폴더 이름.
# (다른 드라이브면 마지막에 '옮기기'가 통째 복사가 돼서 4K·8K 가 느려진다.)
WORKDIR_NAME = ".ssakssak-tmp"


def download_temp_root() -> Path:
    """다운로드 중간 산출물(.part, 조각, 병합 전 트랙)을 두는 임시 폴더.

    저장 폴더에 바로 쓰면 재시도로 버려진 .part 가 사용자 폴더에 그대로 남는다
    (유튜브는 높은 화질부터 시도하다 실패하는 일이 잦아 더 두드러진다).
    최종 파일만 저장 폴더로 옮겨지도록 중간물은 여기에 격리한다."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_ID / "tmp"


def config_path() -> Path:
    """사용자 설정(config.json) 저장 위치. 로그/ffmpeg 캐시와 같은 앱 폴더."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_ID / "config.json"


def find_ffmpeg() -> str | None:
    """앱 소유 cache에서 현재 release pin과 정확히 일치하는 ffmpeg만 반환."""
    target = ffmpeg_cache_dir() / "ffmpeg.exe"
    if not target.is_file() or target.is_symlink():
        return None
    try:
        asset = FfmpegInstaller._resolve_asset()
        FfmpegInstaller._verify_executable(
            target, asset.executable_sha256, asset.executable_size)
        if not FfmpegInstaller._smoke_test(target):
            raise RuntimeError("ffmpeg 실행 확인에 실패했습니다")
    except (OSError, RuntimeError):
        # hash 검증 전에는 절대 실행하지 않는다. PATH·일반 설치 위치·PyInstaller
        # 곁파일도 publisher pin이 없으므로 자동 신뢰하지 않는다.
        log.warning("untrusted or unusable ffmpeg cache ignored", exc_info=True)
        return None
    return str(target.parent)


def is_youtube(url: str) -> bool:
    """유튜브(단축·임베드 도메인 포함) URL 인지. POT 서버는 유튜브에만 필요하다."""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return False
    host = host.split("@")[-1].split(":")[0]
    return (host in ("youtu.be", "youtube.com", "m.youtube.com")
            or host.endswith(".youtube.com")
            or host.endswith(".youtube-nocookie.com"))


def potprovider_dir() -> Path | None:
    """번들된 PO Token 공급자 폴더를 찾는다. 번들 안 돼 있으면 None."""
    bases: list[Path] = []
    if getattr(sys, "frozen", False):
        bases.append(Path(getattr(sys, "_MEIPASS", "")))
        bases.append(Path(sys.executable).parent)
    else:
        bases.append(Path(__file__).parent)
    for b in bases:
        d = b / "potprovider"
        if (d / "node.exe").exists() and (d / "server" / "build" / "main.js").exists():
            return d
    return None


class PotProvider:
    """번들 node.exe 로 PO Token 서버를 띄우고 수명을 관리한다(앱당 1개).

    - 유튜브 URL 을 만났을 때만 지연 기동한다(다른 사이트만 쓰면 아예 안 뜬다).
    - 이미 응답하는 서버가 있으면(앱 중복 실행 등) 그걸 재사용한다.
    - 앱 종료 시 stop() 으로 정리한다.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._dir = potprovider_dir()
        self._ready = False
        if self._dir:
            self._register_plugin()
        else:
            log.warning("POT provider not bundled — 유튜브 다운로드가 막힐 수 있음")

    @property
    def available(self) -> bool:
        return self._dir is not None

    def _register_plugin(self) -> None:
        """번들 플러그인 폴더를 yt-dlp 플러그인 경로에 등록(pip 설치 불필요).
        PyInstaller 배포본에선 네임스페이스 패키지 자동탐색이 안 되므로 필수."""
        try:
            import yt_dlp.globals as g
            path = str(self._dir / "plugin")            # type: ignore[operator]
            if path not in g.plugin_dirs.value:
                g.plugin_dirs.value = [*g.plugin_dirs.value, path]
                log.info("POT plugin dir registered: %s", path)
        except Exception:
            log.exception("failed to register POT plugin dir")

    @staticmethod
    def _ping(timeout: float = 1.0) -> bool:
        try:
            with urllib.request.urlopen(f"{POT_BASE_URL}/ping", timeout=timeout):
                return True
        except Exception:
            return False

    def ensure_running(self) -> bool:
        """서버가 응답 가능한 상태임을 보장. 실패해도 예외 없이 False 만 돌려주고
        다운로드는 계속 진행한다(유튜브 외 사이트는 토큰이 필요 없으므로)."""
        if self._ready and self._ping():
            return True
        with self._lock:
            if self._ping():                 # 이미 떠 있으면 재사용
                self._ready = True
                return True
            if not self._dir:
                return False
            self._spawn()
            deadline = time.monotonic() + POT_STARTUP_TIMEOUT
            while time.monotonic() < deadline:
                if self._ping():
                    self._ready = True
                    log.info("POT server ready")
                    return True
                if self._proc is not None and self._proc.poll() is not None:
                    log.error("POT server exited early (code %s)",
                              self._proc.returncode)
                    return False
                time.sleep(0.3)
            log.error("POT server startup timed out")
            return False

    def _spawn(self) -> None:
        """node.exe 로 서버를 백그라운드 기동(콘솔 창 안 뜨게)."""
        if self._dir is None:
            return
        srv = self._dir / "server"
        try:
            self._proc = subprocess.Popen(
                [str(self._dir / "node.exe"), str(srv / "build" / "main.js"),
                 "--port", str(POT_PORT)],
                cwd=str(srv),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            log.info("POT server spawned (pid %s)", self._proc.pid)
        except OSError:
            log.exception("failed to spawn POT server")
            self._proc = None

    def stop(self) -> None:
        """앱 종료 시 서버 프로세스 정리(좀비 node.exe 방지)."""
        with self._lock:
            proc, self._proc, self._ready = self._proc, None, False
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
        log.info("POT server stopped")


# 앱 전역 인스턴스 (import 시점엔 서버를 띄우지 않는다 — ensure_running() 에서 지연 기동)
POT = PotProvider()


def youtube_extractor_args(client: str | None = None) -> dict:
    """유튜브용 yt-dlp extractor_args — POT 서버 주소만 넘긴다.

    player_client 는 일부러 건드리지 않는다. 직접 고른 클라이언트
    (tv_simply/tv/web/mweb)는 토큰이 있어도 화질이 360p 이하로 잘리거나
    아예 포맷이 비었고, yt-dlp 기본 선택에 맡겨야 144p~2160p 가 다 나온다.
    어떤 클라이언트가 살아 있는지는 유튜브 사정에 따라 계속 바뀌므로,
    client=None(기본)으로 먼저 시도하고 실패하면 사다리를 내려간다."""
    args: dict = {"youtubepot-bgutilhttp": {"base_url": [POT_BASE_URL]}}
    if client:
        args["youtube"] = {"player_client": [client]}
    return args


# ============================================================
#  엔진(yt-dlp) 자동 갱신
# ============================================================

# 상업 배포본은 릴리스 lock에 검토·고정되어 서명된 앱 내부 엔진만 사용한다.
# PyPI metadata와 wheel을 같은 채널에서 받아 실행하는 runtime 갱신은 독립된
# publisher 서명 manifest가 도입되기 전까지 금지한다.
ENGINE_AUTO_UPDATE_ENABLED = False
YTDLP_PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
PYPI_TRUSTED_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})
ENGINE_CHECK_INTERVAL = 12 * 3600      # 확인 주기(초)
ENGINE_NET_TIMEOUT = 20                # 갱신 통신 타임아웃(초)
ENGINE_MAX_WHEEL = 40 * 1024 * 1024    # 휠 다운로드 크기 상한
ENGINE_MAX_MEMBERS = 4_000             # zip bomb / 비정상 wheel 방어
ENGINE_MAX_UNCOMPRESSED = 160 * 1024 * 1024
ENGINE_MAX_MEMBER_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class EngineWheelAsset:
    version: str
    url: str
    filename: str
    size: int
    sha256: str


def engine_update_dir() -> Path:
    """앱이 내려받은 엔진을 두는 쓰기 가능 폴더."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_ID / "engine"


def engine_state_path() -> Path:
    """마지막 확인 시각 기록 (매 실행마다 네트워크를 두드리지 않기 위해)."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_ID / "engine.json"


class EngineUpdater:
    """yt-dlp 를 백그라운드로 최신화한다.

    받아둔 엔진은 '다음 실행'부터 쓰인다(실행 중인 모듈을 갈아끼우지 않는다).
    실패해도 조용히 넘어간다 — 갱신은 부가 기능이고, 실패해도 기존 엔진으로
    계속 동작해야 한다."""

    def __init__(self, current_version: str):
        self._current = current_version
        self._cancel_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start_async(self) -> threading.Thread:
        """독립 서명 runtime manifest가 없으면 network updater를 시작하지 않는다."""
        if not ENGINE_AUTO_UPDATE_ENABLED:
            raise RuntimeError("서명되지 않은 yt-dlp runtime 자동 갱신은 비활성화되어 있습니다")
        thread = threading.Thread(target=self._safe_run, name="engine-updater",
                                  daemon=True)
        self._thread = thread
        thread.start()
        return thread

    def cancel(self) -> None:
        self._cancel_event.set()

    def _safe_run(self) -> None:
        try:
            self.run()
        except Exception:
            log.exception("engine update failed")

    def _due(self) -> bool:
        try:
            data = json.loads(engine_state_path().read_text(encoding="utf-8"))
            last = float(data.get("last_check", 0))
        except (OSError, ValueError, TypeError):
            return True
        return (time.time() - last) >= ENGINE_CHECK_INTERVAL

    def _stamp(self) -> None:
        try:
            p = engine_state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"last_check": time.time()}), encoding="utf-8")
        except OSError:
            log.warning("engine state 기록 실패", exc_info=True)

    @staticmethod
    def _latest_from_pypi() -> EngineWheelAsset | None:
        """PyPI가 지정한 최신 안정판 wheel의 검증 메타데이터를 반환한다."""
        api_url = _require_trusted_https_url(
            YTDLP_PYPI_URL, PYPI_TRUSTED_HOSTS, "PyPI API 주소")
        req = urllib.request.Request(api_url, headers={"User-Agent": HTTP_UA})
        with urllib.request.urlopen(req, timeout=ENGINE_NET_TIMEOUT) as resp:
            _require_trusted_response_url(
                resp, api_url, PYPI_TRUSTED_HOSTS, "PyPI API 리디렉션")
            raw = resp.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise RuntimeError("PyPI 응답이 비정상적으로 큽니다")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("PyPI 응답 형식이 올바르지 않습니다")

        # info.version은 PyPI가 가리키는 최신 안정판이다. releases 전체를 비교해
        # dev/rc/nightly를 자동 설치하지 않는다.
        info = data.get("info")
        version = str(info.get("version") or "") if isinstance(info, dict) else ""
        if re.fullmatch(r"\d+(?:\.\d+){2,3}", version) is None:
            raise RuntimeError("PyPI 안정판 버전 형식이 올바르지 않습니다")
        expected_name = f"yt_dlp-{version}-py3-none-any.whl"
        releases = data.get("releases")
        files = releases.get(version) if isinstance(releases, dict) else None
        matches: list[EngineWheelAsset] = []
        for item in files if isinstance(files, list) else []:
            if not isinstance(item, dict) or item.get("yanked"):
                continue
            if item.get("packagetype") != "bdist_wheel":
                continue
            filename = str(item.get("filename") or "")
            if filename != expected_name:
                continue
            url = _require_trusted_https_url(
                item.get("url"), PYPI_TRUSTED_HOSTS, "yt-dlp wheel 주소")
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            digests = item.get("digests")
            sha = str(digests.get("sha256") or "").lower() \
                if isinstance(digests, dict) else ""
            if not (0 < size <= ENGINE_MAX_WHEEL) or not _valid_sha256(sha):
                raise RuntimeError("yt-dlp wheel의 크기 또는 SHA256 정보가 없습니다")
            matches.append(EngineWheelAsset(version, url, filename, size, sha))
        if len(matches) != 1:
            raise RuntimeError("검증 가능한 yt-dlp wheel이 정확히 하나가 아닙니다")
        return matches[0]

    def run(self) -> None:
        if self._cancel_event.is_set() or not self._due():
            return
        latest = self._latest_from_pypi()
        self._stamp()
        if self._cancel_event.is_set() or latest is None:
            return
        if compare_versions(latest.version, self._current) <= 0:
            log.info("engine up to date (%s)", self._current)
            return

        log.info("engine update: %s -> %s", self._current, latest.version)
        req = urllib.request.Request(latest.url, headers={"User-Agent": HTTP_UA})
        with urllib.request.urlopen(req, timeout=ENGINE_NET_TIMEOUT) as resp:
            _require_trusted_response_url(
                resp, latest.url, PYPI_TRUSTED_HOSTS, "yt-dlp wheel 리디렉션")
            content_length = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
            if content_length:
                try:
                    if int(content_length) != latest.size:
                        raise RuntimeError("yt-dlp wheel 응답 크기가 메타데이터와 다릅니다")
                except ValueError as exc:
                    raise RuntimeError("yt-dlp wheel 응답 크기가 올바르지 않습니다") from exc
            blob = resp.read(latest.size + 1)
        if len(blob) != latest.size:
            raise RuntimeError("yt-dlp wheel 다운로드 크기가 메타데이터와 다릅니다")
        if hashlib.sha256(blob).hexdigest() != latest.sha256:
            raise RuntimeError("yt-dlp wheel SHA256이 일치하지 않습니다")
        if self._cancel_event.is_set():
            return
        self._install(blob, latest.version)

    @staticmethod
    def _install(wheel_bytes: bytes, version: str) -> None:
        """검증된 wheel에서 yt_dlp/만 안전하게 추출하고 rollback 가능하게 교체한다."""
        dest = engine_update_dir()
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="engine-", suffix=".new",
                                        dir=str(dest.parent)))
        backup = dest.with_name(dest.name + ".old")
        moved_old = False
        try:
            root = staging.resolve()
            with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as z:
                infos = z.infolist()
                if len(infos) > ENGINE_MAX_MEMBERS:
                    raise RuntimeError("yt-dlp wheel 항목이 비정상적으로 많습니다")
                total = 0
                seen: set[str] = set()
                package_files = 0
                for info in infos:
                    normalized = info.filename.replace("\\", "/")
                    parts = [p for p in normalized.split("/") if p]
                    if (not parts or normalized.startswith("/") or ".." in parts
                            or info.flag_bits & 0x1):
                        raise RuntimeError("yt-dlp wheel에 안전하지 않은 경로가 있습니다")
                    key = "/".join(parts).lower()
                    if key in seen:
                        raise RuntimeError("yt-dlp wheel에 중복 경로가 있습니다")
                    seen.add(key)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(mode):
                        raise RuntimeError("yt-dlp wheel에 심볼릭 링크가 있습니다")
                    if info.file_size > ENGINE_MAX_MEMBER_BYTES:
                        raise RuntimeError("yt-dlp wheel 항목이 비정상적으로 큽니다")
                    total += info.file_size
                    if total > ENGINE_MAX_UNCOMPRESSED:
                        raise RuntimeError("yt-dlp wheel 압축 해제 크기가 너무 큽니다")
                    if info.is_dir() or not normalized.startswith("yt_dlp/"):
                        continue
                    target = (staging / normalized).resolve()
                    try:
                        target.relative_to(root)
                    except ValueError as exc:
                        raise RuntimeError("yt-dlp wheel 경로가 작업 폴더를 벗어납니다") from exc
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                    package_files += 1

            version_file = staging / "yt_dlp" / "version.py"
            if package_files == 0 or not version_file.is_file():
                raise RuntimeError("yt-dlp wheel에 필수 패키지가 없습니다")
            version_text = version_file.read_text(encoding="utf-8", errors="strict")
            match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]",
                              version_text, re.MULTILINE)
            if match is None or match.group(1) != version:
                raise RuntimeError("yt-dlp wheel 내부 버전이 메타데이터와 다릅니다")
            (staging / ENGINE_VERSION_FILE).write_text(version, encoding="utf-8")

            shutil.rmtree(backup, ignore_errors=True)
            if dest.exists():
                os.replace(dest, backup)
                moved_old = True
            try:
                os.replace(staging, dest)
            except OSError:
                if moved_old and backup.exists() and not dest.exists():
                    os.replace(backup, dest)
                raise
            shutil.rmtree(backup, ignore_errors=True)
            log.info("engine updated to %s (다음 실행부터 적용)", version)
        except Exception:
            log.warning("engine 교체 실패 - 기존 엔진 유지", exc_info=True)
            if moved_old and backup.exists() and not dest.exists():
                try:
                    os.replace(backup, dest)
                except OSError:
                    log.critical("engine rollback 실패: %s", backup, exc_info=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def looks_like_login_error(err: str) -> bool:
    """yt-dlp 오류 메시지가 '로그인/인증 필요'로 보이면 True.
    이때만 쿠키를 자동으로 한 번 더 시도한다(평소엔 쿠키를 건드리지 않음)."""
    e = (err or "").lower()
    keys = (
        "login", "log in", "sign in", "private", "members-only", "member-only",
        "available to this channel's members", "account", "authenticat",
        "cookies", "age-restricted", "age restricted", "confirm your age",
        "로그인", "회원", "비공개",
    )
    return any(k in e for k in keys)


def detect_cookie_browser() -> str | None:
    """실제 쿠키 DB가 있는 브라우저만 반환한다. 명시적 opt-in 뒤에만 호출한다."""
    local_raw = os.environ.get("LOCALAPPDATA")
    appdata_raw = os.environ.get("APPDATA")
    local = Path(local_raw) if local_raw else None
    appdata = Path(appdata_raw) if appdata_raw else None

    def chromium_has_db(root: Path | None, opera: bool = False) -> bool:
        if root is None:
            return False
        profiles = [root] if opera else [root / "Default"]
        if not opera:
            try:
                profiles.extend(p for p in root.glob("Profile *") if p.is_dir())
            except OSError:
                return False
        return any((profile / "Network" / "Cookies").is_file()
                   or (profile / "Cookies").is_file() for profile in profiles)

    candidates: list[tuple[str, bool]] = []
    if local is not None:
        candidates.extend([
            ("chrome", chromium_has_db(local / "Google" / "Chrome" / "User Data")),
            ("edge", chromium_has_db(local / "Microsoft" / "Edge" / "User Data")),
            ("whale", chromium_has_db(local / "Naver" / "Naver Whale" / "User Data")),
            ("brave", chromium_has_db(local / "BraveSoftware" / "Brave-Browser" / "User Data")),
            ("vivaldi", chromium_has_db(local / "Vivaldi" / "User Data")),
        ])
    if appdata is not None:
        firefox_root = appdata / "Mozilla" / "Firefox" / "Profiles"
        try:
            firefox_ok = any(p.is_file() for p in firefox_root.glob("*/cookies.sqlite"))
        except OSError:
            firefox_ok = False
        candidates.extend([
            ("firefox", firefox_ok),
            ("opera", chromium_has_db(
                appdata / "Opera Software" / "Opera Stable", opera=True)),
        ])
    return next((name for name, exists in candidates if exists), None)


def looks_like_drm(err: str) -> bool:
    """오류 메시지가 DRM 보호 콘텐츠로 보이면 True. 이때는 재시도/우회하지 않고
    'DRM 미지원'을 사용자에게 안내한 뒤 중단한다(DRM 우회는 불법이라 미구현)."""
    e = (err or "").lower()
    return any(k in e for k in DRM_HINTS)


def looks_like_output_error(err: str) -> bool:
    """'다 받아놓고 저장하는 데서' 실패한 경우인지 판별.

    네트워크가 아니라 디스크 문제라서, 다른 다운로드 방식으로 재시도해봐야
    똑같이 실패한다. 재시도 사다리를 타지 않고 즉시 원인을 알려야 한다."""
    e = (err or "").lower()
    return any(k in e for k in OUTPUT_ERROR_HINTS)


def friendly_error(err: str) -> str:
    """yt-dlp/OS 의 원문 오류를 사용자가 읽을 수 있는 한 줄로 바꾼다.

    원문을 그대로 띄우면 'Requested format is not available' 처럼 진짜 원인과
    상관없는 영어 문장이 화면에 남는다."""
    if not err:
        return "다운로드에 실패했습니다."
    err = redact_urls_in_text(err)
    if looks_like_output_error(err):
        return ("저장 폴더에 파일을 쓰지 못했습니다 — 같은 이름의 파일이 "
                "재생 중이거나 다른 프로그램에서 열려 있는지 확인해 주세요.")
    e = err.lower()
    if "requested format is not available" in e:
        return "이 화질로는 받을 수 없습니다. 다른 화질로 시도해 주세요."
    if "could not copy" in e and "cookie" in e:
        return "브라우저 쿠키를 읽지 못했습니다 — 브라우저를 닫고 다시 시도해 주세요."
    if "sign in" in e or "login" in e:
        return "로그인이 필요한 영상입니다."
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    return (lines[0] if lines else err)[:200]


# ============================================================
#  서비스: 영상 정보 분석
# ============================================================

@dataclass
class AnalysisResult:
    title: str
    meta: str                 # 한 줄 요약 (플레이리스트 여부도 이 문구에 반영)
    heights: list[int] = field(default_factory=list)
    thumbnail: str = ""       # 미리보기 썸네일 URL (없으면 빈 문자열)


class VideoAnalyzer:
    """yt-dlp로 메타데이터만 가져온다 (다운로드 X). UI 비의존."""

    def probe(self, url: str, cookies_browser: str | None = None) -> AnalysisResult:
        validate_public_http_url(url)
        if cookies_browser is not None and cookies_browser not in COOKIE_BROWSERS:
            raise ValueError("지원하지 않는 브라우저 쿠키 설정입니다")
        opts: dict = {"quiet": True, "no_warnings": True, "skip_download": True,
                      "socket_timeout": SOCKET_TIMEOUT,
                      "cachedir": ytdlp_cache_dir()}
        # 분석 단계에서도 토큰이 있어야 실제 화질 목록이 채워진다.
        if is_youtube(url):
            POT.ensure_running()
            opts["extractor_args"] = youtube_extractor_args()
        if cookies_browser:
            # (browser, profile, keyring, container) 4-튜플이어야 한다.
            opts["cookiesfrombrowser"] = (cookies_browser, None, None, None)
        with _guarded_youtube_dl(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            first = next((e for e in entries if e), None)
            title = info.get("title", "(제목 없음)")
            meta = f"📃 플레이리스트  ·  영상 {len(entries)}개 (전체 일괄 다운로드)"
            return AnalysisResult(
                title=title, meta=meta,
                heights=self._heights(first or {}),
                thumbnail=self._thumb(first or {}) or self._thumb(info),
            )

        title = info.get("title", "(제목 없음)")
        channel = info.get("uploader") or info.get("channel") or ""
        dur = int(info.get("duration") or 0)
        m, s = divmod(dur, 60)
        heights = self._heights(info)
        max_h = max(heights) if heights else None
        max_str = f"최대 {max_h}p" if max_h else "화질 정보 없음"

        parts = []
        if channel:
            parts.append(f"🎬 {channel}")
        parts.append(f"⏱ {m}:{s:02d}")
        parts.append(max_str)
        return AnalysisResult(title=title, meta="  ·  ".join(parts),
                              heights=heights, thumbnail=self._thumb(info))

    @staticmethod
    def _heights(info: dict) -> list[int]:
        """실제 영상 트랙의 '짧은 변' 해상도만 모은다.

        화질 표기(1080p)는 짧은 변 기준 — 세로 1080×1920 영상도 1080p다.
        height 그대로 쓰면 세로 영상에서 최대가 1920으로 잡혀 받을 수 없는
        1440p/2160p 선택지가 뜬다. 미리보기 스토리보드(mhtml)는 제외."""
        formats = info.get("formats") or []
        hs = {
            min(f["height"], f["width"]) if isinstance(f.get("width"), int)
            else f["height"]
            for f in formats
            if isinstance(f.get("height"), int)
            and f.get("vcodec") not in (None, "none")
            and f.get("ext") != "mhtml"
        }
        return sorted(hs)

    @staticmethod
    def _thumb(info: dict) -> str:
        """WebView가 backend pinning 밖에서 원격 이미지를 읽지 않도록 비활성화."""
        return ""


# ============================================================
#  서비스: 다운로드
# ============================================================

# 사용자가 취소했을 때 on_done(err) 로 전달되는 내부 신호값 (실제 에러 아님).
DOWNLOAD_CANCELLED = "__download_cancelled__"


class HarvestError(RuntimeError):
    """완성 파일을 최종 저장 폴더에 확정하지 못했을 때 발생한다.

    네트워크 재시도로 해결할 수 없는 오류이며, 호출자는 성공 처리하거나 작업
    폴더를 삭제하면 안 된다. recovery_dir에는 사용자가 직접 복구할 수 있는
    완성본/중간 산출물이 남아 있다.
    """

    def __init__(self, message: str, recovery_dir: str):
        super().__init__(message)
        self.recovery_dir = recovery_dir


@dataclass
class DownloadConfig:
    url: str
    out_dir: str
    format_str: str
    is_audio_only: bool
    container: str
    ffmpeg_dir: str | None
    cookies_browser: str | None = None   # yt-dlp 브라우저 이름 (None=사용 안 함)
    embed_metadata: bool = True          # 메타데이터 + 썸네일(커버) 삽입
    use_playwright: bool = False         # 2단계 최후폴백. 소스(개인)=True, 배포 exe=False
    target_height: int | None = None     # 선택 화질(짧은 변 px). None=최고화질/음성만


class Downloader:
    """yt-dlp 다운로드 래퍼. progress/완료 콜백 호출."""

    # 저장 폴더로 옮길 때 '빈 이름 고르기 → 옮기기' 를 한 덩어리로 묶는 잠금.
    # 동시 다운로드 두 개가 같은 이름을 동시에 집는 걸 막는다 (클래스 공유).
    _harvest_lock = threading.Lock()

    def __init__(self,
                 on_progress: Callable[[dict], None],
                 on_done: Callable[[bool, str | None], None],
                 on_item_done: Callable[[str, str | None], None] | None = None,
                 on_status: Callable[[str], None] | None = None):
        self._on_progress = on_progress
        self._on_done = on_done
        # 항목(영상) 1개의 후처리까지 끝났을 때 호출. 플레이리스트에서
        # 각 항목을 개별 완료 처리하기 위한 콜백 (vid, 최종 경로 전달).
        self._on_item_done = on_item_done
        # 폴백 재시도/2단계 진입 시 사용자에게 보여줄 상태 문구 콜백 (선택).
        self._on_status = on_status or (lambda _msg: None)
        # 취소와 성공 확정은 이 lock 아래 terminal state 전환 하나로 선형화한다.
        # cancel이 먼저면 수확 결과를 rollback하고, succeeded가 먼저면 이후 cancel은
        # 이미 완료된 결과를 뒤집지 않는다.
        self._terminal_lock = threading.Lock()
        self._terminal_state = "running"
        self._cancel_event = threading.Event()
        # 이번 다운로드 전용 임시 폴더 (run() 이 만들고, 성공/일반 실패 때 지운다).
        self._tmpdir: str | None = None
        # 최종 저장 확정에 실패하면 유일한 완성본이 든 작업 폴더를 보존한다.
        self._preserve_tmpdir = False
        # 최종 저장 폴더 (후처리 훅이 임시 경로를 최종 경로로 바꿀 때 쓴다).
        self._out_dir: str | None = None
        # 후처리 훅이 보고한 실제 해상도 — 완료 UI에서 '요청 화질보다 낮게
        # 저장됐다'를 표시하는 데 쓴다 (셀렉터 폴백으로 작은 파일이 나와도
        # 조용히 넘어가지 않도록).
        self._dims_by_name: dict[str, tuple[int, int]] = {}
        self._last_dims: tuple[int, int] | None = None
        # 사용자가 고른 화질(짧은 변 px). run() 이 cfg에서 채운다.
        self._target_height: int | None = None

    def cancel(self) -> None:
        """성공 확정 전 취소만 terminal state에 반영한다."""
        with self._terminal_lock:
            if self._terminal_state != "running":
                return
            self._terminal_state = "cancelled"
            self._cancel_event.set()

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled()

    def _commit_success(self) -> bool:
        """cancel과 경쟁하는 단 하나의 성공 선형화 지점."""
        with self._terminal_lock:
            if self._terminal_state != "running" or self._cancel_event.is_set():
                return False
            self._terminal_state = "succeeded"
            return True

    def _progress_hook(self, d: dict) -> None:
        """yt-dlp progress hook 래퍼. 취소 요청이 있으면 DownloadCancelled 을
        던져 다운로드를 즉시 멈추고, 아니면 앱 콜백으로 위임한다."""
        if self._cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled()
        self._on_progress(d)

    def _postprocessor_hook(self, d: dict) -> None:
        """후처리 중에도 취소 신호를 존중하고 임시 완료는 UI에 확정하지 않는다."""
        if self._cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled()
        if d.get("status") != "finished" or self._on_item_done is None:
            return
        info = d.get("info_dict") or {}
        vid = str(info.get("id") or "")
        if vid:
            # 실제 저장 해상도를 기록한다 — 병합된 출력의 width/height라
            # 셀렉터가 낮은 포맷을 골랐을 때도 실제 결과가 드러난다.
            w, h = info.get("width"), info.get("height")
            if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                self._last_dims = (w, h)
                fp = info.get("filepath")
                if fp:
                    self._dims_by_name[os.path.basename(fp)] = (w, h)
            # 후처리 결과의 최종 경로. 진행률 훅이 잡은 임시 조각 파일명 대신
            # 이걸 써야 '재생/폴더 열기'가 올바른 최종 파일을 가리킨다.
            self._on_item_done(vid, self._final_path(info.get("filepath")))

    def _final_path(self, filepath: str | None) -> str | None:
        """후처리 훅이 알려준 경로를 '옮겨진 뒤'의 예상 경로로 바꾼다.

        yt-dlp 는 작업 폴더 안에서만 일하므로(_base_opts 의 paths) 훅 시점의
        경로는 아직 작업 폴더를 가리킨다. 그대로 UI 에 넘기면 '재생/폴더 열기'가
        곧 사라질 파일을 가리키게 되므로 파일명만 떼어 저장 폴더에 붙인다.
        여기서 만든 건 어디까지나 예상값이고, 실제로 옮긴 뒤 _harvest() 가
        진짜 경로로 다시 알려준다(이름이 겹쳐 '(2)' 가 붙었을 수 있다)."""
        if not filepath or not self._out_dir or not self._tmpdir:
            return filepath
        try:
            if (os.path.normcase(os.path.dirname(os.path.abspath(filepath)))
                    == os.path.normcase(os.path.abspath(self._tmpdir))):
                return os.path.join(self._out_dir, os.path.basename(filepath))
        except (OSError, ValueError):
            pass
        return filepath

    @staticmethod
    def _free_path(out_dir: str, name: str) -> Path:
        """저장 폴더에서 아직 안 쓰인 이름을 고른다.

        같은 영상을 두 번 받아도 덮어쓰지 않고 '제목 (2).mp4' 로 남는다
        (브라우저 다운로드와 같은 방식)."""
        base = Path(out_dir) / name
        if not base.exists():
            return base
        stem, suffix = base.stem, base.suffix
        for n in range(2, 1000):
            cand = base.with_name(f"{stem} ({n}){suffix}")
            if not cand.exists():
                return cand
        return base.with_name(f"{stem} ({os.getpid()}){suffix}")

    def _publish_completed_file(self, src: Path, out_dir: str) -> Path:
        """완성본을 create-no-replace로 publish한다; 원본은 성공 확정까지 유지."""
        # 같은 volume의 일반 파일 시스템에서는 hard link가 즉시·원자적이며 기존
        # 이름을 덮어쓰지 않는다. 원본은 terminal success 전까지 그대로 남는다.
        for _ in range(1000):
            self._raise_if_cancelled()
            dest = self._free_path(out_dir, src.name)
            try:
                os.link(src, dest)
                return dest
            except FileExistsError:
                continue
            except OSError:
                break

        # hard link를 지원하지 않는 volume이면 목적지 폴더의 비-media staging에
        # 복사·fsync한 뒤 원자적으로 publish한다. 최종 이름에는 partial이 없다.
        fd, stage_name = tempfile.mkstemp(
            prefix=".ssak-harvest-", suffix=".part", dir=out_dir)
        stage = Path(stage_name)
        try:
            with open(src, "rb") as source, os.fdopen(fd, "wb") as target:
                fd = -1
                while True:
                    self._raise_if_cancelled()
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            self._raise_if_cancelled()
            for _ in range(1000):
                dest = self._free_path(out_dir, src.name)
                try:
                    if os.name == "nt":
                        # Windows rename은 목적지가 있으면 실패하므로 no-replace다.
                        os.rename(stage, dest)
                    else:
                        # POSIX rename은 덮어쓰므로 hard-link create를 사용한다.
                        os.link(stage, dest)
                        stage.unlink()
                    return dest
                except FileExistsError:
                    continue
            raise OSError("저장 폴더에서 빈 파일명을 확보하지 못했습니다")
        finally:
            if fd >= 0:
                os.close(fd)
            with contextlib.suppress(OSError):
                stage.unlink()

    @staticmethod
    def _rollback_published(published: list[tuple[Path, Path]]) -> bool:
        """원본은 아직 작업 폴더에 있으므로 publish된 link/copy만 제거한다."""
        failed = False
        for _source, destination in reversed(published):
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                failed = True
                log.exception("저장 확정 rollback 실패: %s", destination)
        return failed

    def _harvest(self, out_dir: str) -> list[str]:
        """완성 파일 전체를 취소와 선형화된 하나의 transaction으로 확정한다."""
        if not self._tmpdir:
            raise HarvestError("안전한 작업 폴더가 없어 결과를 확정할 수 없습니다", "")

        recovery_dir = self._tmpdir
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            work_root = Path(recovery_dir).resolve(strict=True)
            produced = sorted(
                p for p in Path(recovery_dir).iterdir()
                if p.is_file()
                and not p.is_symlink()
                and p.resolve(strict=True).parent == work_root
                and p.suffix.lower() in MEDIA_EXTENSIONS
                and not p.name.lower().endswith((".part", ".ytdl"))
                and not p.stem.lower().endswith(".temp")
                and ".part-" not in p.name.lower()
            )
        except OSError as exc:
            self._preserve_tmpdir = True
            log.exception("작업 폴더를 읽거나 저장 폴더를 준비하지 못했습니다")
            raise HarvestError(
                f"완성 파일을 저장하지 못했습니다. 복구 폴더: {recovery_dir}",
                recovery_dir,
            ) from exc

        if not produced:
            self._preserve_tmpdir = True
            raise HarvestError(
                f"다운로드 결과 파일을 확인하지 못했습니다. 복구 폴더: {recovery_dir}",
                recovery_dir,
            )

        published: list[tuple[Path, Path]] = []
        # 이름 선택, publish, cancel/success 선형화를 한 구간으로 묶는다.
        with self._harvest_lock:
            try:
                for src in produced:
                    self._raise_if_cancelled()
                    dest = self._publish_completed_file(src, out_dir)
                    published.append((src, dest))
                    self._raise_if_cancelled()
                if not self._commit_success():
                    raise yt_dlp.utils.DownloadCancelled()
            except yt_dlp.utils.DownloadCancelled:
                rollback_failed = self._rollback_published(published)
                if rollback_failed:
                    self._preserve_tmpdir = True
                    raise HarvestError(
                        "취소했지만 일부 파일을 저장 폴더에서 회수하지 못했습니다. "
                        f"복구 폴더: {recovery_dir}", recovery_dir)
                raise
            except Exception as exc:
                rollback_failed = self._rollback_published(published)
                self._preserve_tmpdir = True
                extra = (" 일부 파일은 저장 폴더에도 남아 있을 수 있습니다."
                         if rollback_failed else "")
                log.exception("완성 파일 저장 transaction이 실패했습니다")
                raise HarvestError(
                    "완성 파일을 저장 폴더로 옮기지 못했습니다. "
                    f"복구 폴더: {recovery_dir}.{extra}", recovery_dir) from exc

        # succeeded가 cancel보다 먼저 확정된 뒤에만 작업 원본을 제거하고 publish한다.
        committed_paths = [str(dest) for _, dest in published]
        for src, _dest in published:
            with contextlib.suppress(OSError):
                src.unlink()
        if self._on_item_done is not None:
            for src, dest in published:
                try:
                    dims = (self._dims_by_name.get(src.name)
                            or self._last_dims)
                    self._on_item_done("", str(dest), dims)
                except Exception:
                    log.exception("완료 콜백 처리 실패: %s", dest)
        for path in committed_paths:
            log.info("saved: %s", Path(path).name)
        return committed_paths

    def run(self, cfg: DownloadConfig) -> None:
        """단계적 폴백으로 다운로드 성공률을 높인다.

        1단계(yt-dlp): 기본 → 브라우저 쿠키 → 브라우저 헤더 → 포맷 낮춤 순으로
                       하나라도 성공할 때까지 재시도.
        2단계(yt-dlp 미지원): 페이지 HTML 에서 HLS/DASH 매니페스트를 직접 찾아
                       yt-dlp 에 재투입. (옵트인 시) Playwright 로 네트워크 가로채기.
        DRM 감지 시: 우회하지 않고 즉시 안내 후 중단."""
        self._preserve_tmpdir = False
        self._out_dir = cfg.out_dir
        self._target_height = cfg.target_height
        if self._cancel_event.is_set():
            # start_download() 직후 worker가 돌기 전에 취소된 경우도 존중한다.
            self._on_done(False, DOWNLOAD_CANCELLED)
            return
        self._tmpdir = self._make_workdir(cfg.out_dir)
        if not self._tmpdir:
            self._on_done(
                False,
                "안전한 다운로드 작업 폴더를 만들지 못했습니다. "
                "저장 폴더 권한과 디스크 공간을 확인해 주세요.",
            )
            return
        try:
            self._run_inner(cfg)
        finally:
            if self._tmpdir:
                tmpdir = self._tmpdir
                parent = Path(tmpdir).parent
                if self._preserve_tmpdir:
                    log.error("복구를 위해 작업 폴더를 보존합니다: %s", tmpdir)
                else:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    # 저장 폴더 안에 만든 작업 폴더라면 껍데기도 치운다. rmdir 은
                    # 비었을 때만 지우므로, 동시에 받는 다른 다운로드는 안 건드린다.
                    if parent.name == WORKDIR_NAME:
                        with contextlib.suppress(OSError):
                            parent.rmdir()
                self._tmpdir = None

    @staticmethod
    def _same_volume(a: str, b: str) -> bool:
        """두 경로가 같은 드라이브에 있는지. 판단이 안 되면 False(=보수적)."""
        try:
            da = os.path.splitdrive(os.path.abspath(a))[0]
            db = os.path.splitdrive(os.path.abspath(b))[0]
        except (OSError, ValueError):
            return False
        return bool(da) and da.lower() == db.lower()

    def _make_workdir(self, out_dir: str) -> str | None:
        """이번 다운로드 전용 작업 폴더를 만든다 (동시 다운로드끼리 격리).

        저장 폴더와 같은 드라이브에 두어야 마지막에 옮기는 게 즉시 끝나는
        '이름 바꾸기'가 된다. 다른 드라이브면 통째로 복사라서 4K·8K 영상에선
        눈에 띄게 느려진다. 그래서 기본은 앱 폴더(%LOCALAPPDATA%)를 쓰되,
        저장 폴더가 다른 드라이브일 때만 저장 폴더 안에 만든다.
        어느 쪽이든 끝나면 run() 이 통째로 지운다.
        만들지 못하면 안전한 publish/rollback을 보장할 수 없으므로 실패한다."""
        app_root = download_temp_root()
        roots = [app_root]
        if not self._same_volume(str(app_root), out_dir):
            # 저장 폴더가 다른 드라이브 → 복사를 피하려고 그 안에 만든다.
            # 다 받고 나면 run() 이 이 껍데기까지 지운다.
            roots.insert(0, Path(out_dir) / WORKDIR_NAME)
        for root in roots:
            try:
                root.mkdir(parents=True, exist_ok=True)
                return tempfile.mkdtemp(prefix="dl-", dir=str(root))
            except OSError:
                log.warning("작업 폴더 생성 실패: %s", root, exc_info=True)
        log.error("안전한 다운로드 작업 폴더를 만들지 못했습니다")
        return None

    def _run_inner(self, cfg: DownloadConfig) -> None:
        """run() 본체. 임시 폴더 정리는 run() 이 책임진다."""
        base = self._base_opts(cfg)
        # 첫 시도의 오류를 따로 붙든다. 마지막 시도는 'best/worst 로 낮춰보기'
        # 같은 최후수단이라, 유튜브에선 늘 '이 화질은 못 받는다'로 끝난다.
        # 그 메시지를 그대로 띄우면 진짜 원인(예: 저장 실패)이 가려진다.
        first_err: str | None = None
        last_err: str | None = None

        # ── 1단계: 옵션을 바꿔가며 재시도 ──────────────────────
        for i, (label, extra) in enumerate(self._attempt_plan(cfg)):
            if self._cancel_event.is_set():
                self._on_done(False, DOWNLOAD_CANCELLED)
                return
            if i > 0:                       # 첫 시도는 조용히, 재시도부터 안내
                self._on_status(f"{label}…")
            try:
                log.info("download attempt %d: %s", i + 1, label)
                with _guarded_youtube_dl({**base, **extra}) as ydl:
                    ydl.download([cfg.url])
                self._harvest(cfg.out_dir)
                self._on_done(True, None)
                return
            except HarvestError as e:
                # 네트워크 문제가 아니라 최종 저장 확정 실패다. 재시도하면 대용량
                # 영상을 다시 받을 뿐이며 보존된 완성본까지 위험해질 수 있다.
                self._on_done(False, str(e))
                return
            except yt_dlp.utils.DownloadCancelled:
                log.info("download cancelled by user")
                self._on_done(False, DOWNLOAD_CANCELLED)
                return
            except Exception as e:
                msg = str(e)
                if looks_like_drm(msg):
                    log.info("DRM content detected; bypass not attempted")
                    self._on_done(False, "DRM 보호 콘텐츠는 지원하지 않습니다.")
                    return
                if first_err is None:
                    first_err = msg
                last_err = msg
                safe_msg = redact_urls_in_text(msg)
                log.info("attempt failed (%s): %s", label,
                         (safe_msg.splitlines()[-1] if safe_msg else "")[:160])
                # 디스크에 못 쓴 거라면 방식을 바꿔 받아봐야 똑같이 실패한다.
                if looks_like_output_error(msg):
                    log.error("output write failed; stopping retries")
                    self._on_done(False, friendly_error(msg))
                    return

        # ── 2단계: yt-dlp 가 전부 실패 → 직접 스트림 탐색 ──────
        if not self._cancel_event.is_set():
            try:
                if self._try_stage2(cfg, base):
                    self._harvest(cfg.out_dir)
                    self._on_done(True, None)
                    return
            except HarvestError as e:
                self._on_done(False, str(e))
                return
            except yt_dlp.utils.DownloadCancelled:
                self._on_done(False, DOWNLOAD_CANCELLED)
                return
            except Exception:
                log.exception("stage2 fallback error")

        if self._cancel_event.is_set():
            self._on_done(False, DOWNLOAD_CANCELLED)
        else:
            log.error("all download attempts failed")
            self._on_done(False, friendly_error(first_err or last_err or ""))

    def _base_opts(self, cfg: DownloadConfig) -> dict:
        """모든 시도가 공유하는 기본 yt-dlp 옵션 (포맷/쿠키/헤더는 시도별로 덮음)."""
        if not self._tmpdir:
            raise RuntimeError("안전한 다운로드 작업 폴더가 준비되지 않았습니다")
        opts: dict = {
            "outtmpl": "%(title)s.%(ext)s",
            # 받기·합치기·후처리를 전부 이번 실행 전용 작업 폴더 안에서 끝낸다.
            # 저장 폴더에는 완성된 파일만 _harvest()가 transaction으로 publish한다.
            "paths": {"home": self._tmpdir, "temp": self._tmpdir},
            "noplaylist": False,
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._postprocessor_hook],
            "quiet": True,
            "no_warnings": True,
            "concurrent_fragment_downloads": 4,
            "retries": 10,
            "fragment_retries": 10,
            "socket_timeout": SOCKET_TIMEOUT,
            "cachedir": ytdlp_cache_dir(),
        }
        if cfg.ffmpeg_dir:
            opts["ffmpeg_location"] = cfg.ffmpeg_dir
        if cfg.target_height:
            # res=min(width,height): 짧은 변 기준으로 선택 화질까지 랭킹한다.
            # 'height<=N' 필터가 세로 영상(Shorts)에서 FHD 스트림을 걸러낸 채
            # 480p를 고르던 버그를 고치는 핵심 — 가로/세로 무관하게 동작한다.
            opts["format_sort"] = build_format_sort(cfg.target_height)
        # 유튜브는 PO Token 이 없으면 스트림 자체를 안 준다 → 서버 지연 기동.
        if is_youtube(cfg.url):
            if not POT.ensure_running():
                log.warning("POT server unavailable — 유튜브 다운로드가 실패할 수 있음")
            opts["extractor_args"] = youtube_extractor_args()
        self._configure_output(cfg, opts)
        return opts

    @staticmethod
    def _headers_for(url: str) -> dict:
        """사이트가 직링크 접근을 막을 때 쓰는 브라우저 위장 헤더."""
        try:
            p = urllib.parse.urlparse(url)
            origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""
        except ValueError:
            origin = ""
        h = {"User-Agent": BROWSER_UA}
        if origin:
            h["Referer"] = origin + "/"
            h["Origin"] = origin
        return h

    def _attempt_plan(self, cfg: DownloadConfig) -> list[tuple[str, dict]]:
        """시도 순서(라벨, 덮어쓸 옵션)를 만든다. 쉬운 것부터 점점 강하게."""
        fmt = cfg.format_str
        headers = self._headers_for(cfg.url)
        # 쿠키는 분석 중 로그인 필요가 확인되고 사용자가 명시적으로 허용했을
        # 때 전달된 브라우저만 쓴다. 다운로드 단계에서 임의로 쿠키 DB를 탐색하지 않는다.
        cookie = cfg.cookies_browser if cfg.cookies_browser in COOKIE_BROWSERS else None
        cookie_opt = (
            {"cookiesfrombrowser": (cookie, None, None, None)} if cookie else {}
        )

        plan: list[tuple[str, dict]] = []
        # 1) 기본 (분석 때 쿠키가 필요했던 경우엔 그 쿠키 포함)
        first: dict = {"format": fmt}
        if cfg.cookies_browser:
            first["cookiesfrombrowser"] = (cfg.cookies_browser, None, None, None)

        if is_youtube(cfg.url):
            # 유튜브는 '어떤 재생 클라이언트로 물어보느냐'가 성패를 가른다.
            # 기본 선택은 화질 목록이 가장 풍부하지만 스트림이 403 으로 끊기는
            # 경우가 잦고, tv_simply 등은 끝까지 받아지는 대신 화질이 적게
            # 잡힐 때가 있다. 그래서 화질 좋은 쪽부터 순서대로 내려간다.
            for client in YOUTUBE_CLIENT_LADDER:
                extra = {**first, "extractor_args": youtube_extractor_args(client)}
                label = ("기본 다운로드" if client is None
                         else f"다른 방식으로 재시도 ({client})")
                plan.append((label, extra))
        else:
            plan.append(("기본 다운로드", first))
        # 2) 브라우저 헤더 (쿠키 없이!) — 쿠키 DB 가 잠겨도(브라우저 실행 중) 영향 없음.
        #    쿠키 시도가 실패해도 비-쿠키 시도들은 항상 살아 있도록 분리한다.
        plan.append(("브라우저 헤더로 재시도", {"format": fmt, "http_headers": headers}))
        # 3) 브라우저 쿠키 (쓸 쿠키가 있고 1단계에서 안 썼을 때만)
        if cookie_opt and not cfg.cookies_browser:
            plan.append(("브라우저 쿠키로 재시도", {"format": fmt, **cookie_opt}))
            plan.append(("쿠키+헤더로 재시도",
                         {"format": fmt, "http_headers": headers, **cookie_opt}))
        # 4) 포맷 낮춰 재시도 (쿠키 없이; 영상만 — 음성은 의미 없음)
        if not cfg.is_audio_only:
            plan.append(("호환 포맷으로 재시도", {"format": "best", "http_headers": headers}))
            # res:N 정렬이 남아 있으면 worst가 '기준 화질에서 가장 먼' 포맷
            # (오히려 4K)을 고를 수 있어 기본 정렬로 되돌려 진짜 최소를 받는다.
            plan.append(("최저 화질로 재시도", {
                "format": "worst", "format_sort": [], "http_headers": headers}))
        return plan

    def _try_stage2(self, cfg: DownloadConfig, base: dict) -> bool:
        """yt-dlp 가 사이트를 인식 못 했을 때의 폴백.

        (a) 페이지 HTML 에서 .m3u8/.mpd 를 직접 찾아 그 매니페스트를 yt-dlp 에
            재투입한다 (HLS 세그먼트 다운로드와 ffmpeg 병합은 yt-dlp 가 처리).
        (b) (소스·개인 실행 시에만) Playwright 로 실제 페이지를 띄워 네트워크
            요청에서 스트림 URL 을 가로챈다. 배포 exe 는 cfg.use_playwright=False."""
        # (a) HTML 직접 스캔
        self._on_status("스트림 직접 탐색 중…")
        manifest = self._scan_manifest(cfg.url)
        if manifest and not self._cancel_event.is_set():
            log.info("stage2(a): manifest found, retrying via yt-dlp")
            if self._download_manifest(manifest, cfg, base):
                return True

        # (b) Playwright (옵트인)
        if cfg.use_playwright and not self._cancel_event.is_set():
            self._on_status("브라우저로 스트림 탐색 중…")
            manifest = self._playwright_sniff(cfg.url)
            if manifest and not self._cancel_event.is_set():
                log.info("stage2(b): manifest sniffed via playwright")
                if self._download_manifest(manifest, cfg, base):
                    return True
        return False

    @staticmethod
    def _scan_manifest(page_url: str) -> str | None:
        """public 페이지 HTML에서 public HLS/DASH 링크를 찾는다 (HLS 우선)."""
        try:
            page_url = validate_public_http_url(page_url)
        except RuntimeError:
            return None
        req = urllib.request.Request(page_url, headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml,*/*",
        })
        try:
            with _open_public_url(req, timeout=SOCKET_TIMEOUT) as resp:
                raw = resp.read(5 * 1024 * 1024 + 1)
            if len(raw) > 5 * 1024 * 1024:
                return None
            html = raw.decode("utf-8", "replace").replace("\\/", "/")
        except (urllib.error.URLError, OSError, ValueError, RuntimeError):
            return None

        m3u8: list[str] = []
        mpd: list[str] = []
        seen: set[str] = set()
        for candidate in MANIFEST_RE.findall(html):
            candidate = candidate.replace("&amp;", "&")
            try:
                candidate = validate_public_http_url(candidate)
            except RuntimeError:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            (m3u8 if ".m3u8" in candidate.lower() else mpd).append(candidate)
        picks = m3u8 or mpd
        return picks[0] if picks else None

    def _download_manifest(self, manifest_url: str,
                           cfg: DownloadConfig, base: dict) -> bool:
        """찾은 매니페스트(HLS/DASH)를 yt-dlp 로 받는다. 취소는 상위로 전파."""
        opts = {**base, "format": "best", "http_headers": self._headers_for(cfg.url)}
        try:
            manifest_url = validate_public_http_url(manifest_url)
            with _guarded_youtube_dl(opts) as ydl:
                ydl.download([manifest_url])
            return True
        except yt_dlp.utils.DownloadCancelled:
            raise
        except Exception as e:
            msg = redact_urls_in_text(e)
            log.info("manifest download failed: %s",
                     (msg.splitlines()[-1] if msg else "")[:160])
            return False

    @staticmethod
    def _playwright_sniff(page_url: str) -> str | None:
        """비활성화된 개발용 폴백. 직접 호출돼도 public URL만 허용한다."""
        try:
            page_url = validate_public_http_url(page_url)
        except RuntimeError:
            return None
        try:
            from playwright.sync_api import sync_playwright   # 지연 import (옵트인)
        except ImportError:
            log.warning("playwright 미설치 — playwright 폴백 건너뜀 "
                        "(설치: pip install playwright && playwright install)")
            return None
        found: list[str] = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(user_agent=BROWSER_UA)

                def _on_response(resp) -> None:
                    u = resp.url
                    if ".m3u8" in u.lower() or ".mpd" in u.lower():
                        try:
                            found.append(validate_public_http_url(u))
                        except RuntimeError:
                            pass

                page.on("response", _on_response)
                try:
                    page.goto(page_url, wait_until="networkidle", timeout=30000)
                except Exception:
                    pass   # 타임아웃이어도 그때까지 잡은 URL 은 활용
                browser.close()
        except Exception as e:
            log.info("playwright sniff failed: %s", redact_urls_in_text(e))
            return None
        for u in found:
            if ".m3u8" in u.lower():
                return u
        return found[0] if found else None

    @staticmethod
    def _configure_output(cfg: DownloadConfig, opts: dict) -> None:
        """출력 포맷 + 후처리 파이프라인을 opts 에 채운다.

        후처리 순서(FFmpegMetadata → EmbedThumbnail)는 yt-dlp CLI 가 생성하는
        순서와 동일하게 맞춘다."""
        pps: list[dict] = []
        if cfg.is_audio_only:
            pps.append({"key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3", "preferredquality": "192"})
        else:
            opts["merge_output_format"] = cfg.container

        if cfg.embed_metadata:
            opts["writethumbnail"] = True
            pps.append({"key": "FFmpegMetadata",
                        "add_chapters": True, "add_metadata": True})
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

        opts["postprocessors"] = pps


# ============================================================
#  서비스: 업데이트 체크 (킬스위치 포함)
# ============================================================

@dataclass
class UpdateDecision:
    """unsigned version.json을 실행 권한 없는 UI 알림으로 환원한 결과."""
    kind: str           # "new_version" | "uptodate"
    message: str = ""
    # 원격 JSON 값이 아니라 로컬 repository+검증된 version으로 만든 release page.
    download_url: str | None = None
    latest_version: str = ""
    release_notes: str = ""


class UpdateChecker:
    """원격 version.json을 읽어 UpdateDecision 으로 환원."""

    def __init__(self, url: str = VERSION_INFO_URL,
                 timeout: float = VERSION_CHECK_TIMEOUT,
                 current_version: str = APP_VERSION):
        self.url = url
        self.timeout = timeout
        self.current_version = current_version

    def fetch_decision(self) -> UpdateDecision | None:
        """네트워크/JSON 오류 시 None 반환 (앱 시작을 막지 않음)."""
        if "<your-account>" in self.url:
            # 깃 저장소 URL이 아직 placeholder → 체크 스킵
            return None
        try:
            req = urllib.request.Request(
                self.url,
                headers={"User-Agent": HTTP_UA},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(VERSION_JSON_MAX_BYTES + 1)
            if len(raw) > VERSION_JSON_MAX_BYTES:
                log.warning("version.json oversized, ignoring")
                return None
            data = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            log.info("update check skipped (offline or bad json)")
            return None
        except Exception:
            log.exception("update check unexpected error")
            return None

        try:
            return self._interpret(data)
        except (TypeError, ValueError, RuntimeError):
            log.warning("update metadata rejected", exc_info=True)
            return None

    def _interpret(self, data: dict) -> UpdateDecision:
        """unsigned manifest는 새 버전 알림만 제어하며 실행 정책은 제어하지 않는다."""
        if not isinstance(data, dict):
            raise ValueError("version.json 최상위는 객체여야 합니다")
        current = _normalize_release_version(self.current_version)
        latest = _normalize_release_version(data.get("latest_version", current))

        # min_supported_version, killswitch, download_url, installer_*는 모두 무시한다.
        # 알림을 클릭했을 때 갈 주소도 로컬에 고정된 공식 저장소와 정규화한 버전으로
        # 구성하므로 unsigned JSON이 임의 host나 실행 파일을 지정할 수 없다.
        if compare_versions(current, latest) < 0:
            release_page = f"https://github.com/{GITHUB_RELEASE_REPO}/releases/tag/v{latest}"
            return UpdateDecision(
                kind="new_version",
                latest_version=latest,
                release_notes=str(data.get("release_notes", ""))[:2000],
                download_url=release_page,
            )

        return UpdateDecision(kind="uptodate")


# ============================================================
#  서비스: 앱 자동 업데이트 (설치본을 직접 받아 조용히 재설치)
# ============================================================

def app_update_dir() -> Path:
    """새 설치본을 받아두는 폴더."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_ID / "update"


@dataclass
class AppUpdateProgress:
    stage: str        # "resolving" | "downloading" | "verifying" | "launching" | "error"
    pct: float = 0.0
    message: str = ""


@dataclass(frozen=True)
class InstallerAsset:
    """신뢰할 설치본의 주소와 필수 검증 메타데이터."""
    url: str
    size: int = 0
    sha256: str = ""
    version: str = ""


class AppUpdater:
    """새 버전 설치본을 앱이 직접 받아 무인 설치한다 (브라우저를 거치지 않음).

    설치본 주소는 릴리스 API 에서 찾는다. 배포할 때 version.json 의 버전만
    올리면 되고 해시를 손으로 적어 넣을 일이 없다.

    무결성 검증은 크기·SHA256·PE 형식·Authenticode 서명자·ProductVersion이
    모두 일치할 때만 통과한다. 메타데이터나 인증서 설정이 하나라도 없으면
    자동 설치보다 안전을 우선해 fail-closed로 중단한다.

    설치본은 관리자 권한이 필요 없다 (installer.iss 의 PrivilegesRequired=lowest,
    %LOCALAPPDATA%\\Programs 에 설치) → UAC 창 없이 조용히 끝난다."""

    API_URL_FMT = "https://api.github.com/repos/{repo}/releases/tags/{tag}"
    API_TIMEOUT = 10               # 초
    API_MAX_BYTES = 1_000_000      # 릴리스 JSON 응답 크기 상한 (메모리 보호)
    DOWNLOAD_TIMEOUT = 60          # 초
    # 설치본 크기 sanity 범위. Node 토큰 서버를 포함하므로 넉넉한 상한을 둔다.
    MIN_EXE_BYTES = 5 * 1024 * 1024
    MAX_EXE_BYTES = 400 * 1024 * 1024
    PROGRESS_INTERVAL = 0.15       # 초 — UI 갱신 주기 (evaluate_js 폭주 방지)

    # Inno Setup 무인 설치 인자.
    #   /SILENT            진행 창만 띄우고 마법사는 생략 (완전 무음보다 안심됨)
    #   /CLOSEAPPLICATIONS 구버전이 아직 떠 있어도 파일을 교체할 수 있게
    #   /NORESTART         재부팅 요구 금지
    #   /AUTORUN=1         설치가 끝나면 새 버전을 다시 띄운다 (installer.iss 가 읽음)
    SETUP_ARGS = ["/SILENT", "/CLOSEAPPLICATIONS", "/NORESTART", "/AUTORUN=1"]

    def __init__(self, repo: str = GITHUB_RELEASE_REPO):
        self.repo = repo

    # ---------------- 1) 무엇을 받을지 확정 ----------------

    def resolve(self, decision: UpdateDecision) -> InstallerAsset:
        """로컬에 고정된 공식 저장소의 canonical Setup만 fail-closed로 확정한다."""
        version = _normalize_release_version(decision.latest_version)
        # unsigned version.json의 installer URL/size/hash override는 받지 않는다.
        # GitHub API metadata가 변조돼도 최종 실행 전 publisher Authenticode pin을
        # 별도로 검증한다.
        return self._from_api(version)

    def _from_api(self, latest_version: str) -> InstallerAsset:
        """릴리스에서 정확한 canonical Setup 하나만 선택한다."""
        version = _normalize_release_version(latest_version)
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo) is None:
            raise RuntimeError("릴리스 저장소 식별자가 올바르지 않습니다")
        tag = f"v{version}"
        expected_name = CANONICAL_SETUP_NAME_FMT.format(version=version)
        url = self.API_URL_FMT.format(repo=self.repo,
                                      tag=urllib.parse.quote(tag, safe=""))
        _require_trusted_https_url(url, TRUSTED_UPDATE_HOSTS, "릴리스 API 주소")
        req = urllib.request.Request(
            url, headers={"User-Agent": HTTP_UA,
                          "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=self.API_TIMEOUT) as resp:
            _require_trusted_response_url(
                resp, url, TRUSTED_UPDATE_HOSTS, "릴리스 API 리디렉션")
            raw = resp.read(self.API_MAX_BYTES + 1)
        if len(raw) > self.API_MAX_BYTES:
            raise RuntimeError("릴리스 API 응답이 비정상적으로 큽니다")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("릴리스 API 응답 형식이 올바르지 않습니다")

        candidates = [a for a in (data.get("assets") or [])
                      if isinstance(a, dict) and a.get("name") == expected_name]
        if len(candidates) != 1:
            raise RuntimeError(
                f"{tag} 릴리스에 {expected_name} 파일이 정확히 하나 있어야 합니다")
        item = candidates[0]
        digest = str(item.get("digest") or "")
        sha = (digest.split(":", 1)[1].strip().lower()
               if digest.lower().startswith("sha256:") else "")
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        asset = InstallerAsset(
            url=str(item.get("browser_download_url") or ""),
            size=size,
            sha256=sha,
            version=version,
        )
        self._validate_asset(asset)
        log.info("app update: canonical asset %s (%s bytes, sha256=%s)",
                 expected_name, asset.size, asset.sha256)
        return asset

    @classmethod
    def _validate_asset(cls, asset: InstallerAsset) -> None:
        version = _normalize_release_version(asset.version)
        url = _require_trusted_https_url(
            asset.url, TRUSTED_UPDATE_HOSTS, "설치 파일 주소")
        expected_name = CANONICAL_SETUP_NAME_FMT.format(version=version)
        if Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name != expected_name:
            raise RuntimeError("설치 파일명이 canonical Setup 이름과 다릅니다")
        if not (cls.MIN_EXE_BYTES <= asset.size <= cls.MAX_EXE_BYTES):
            raise RuntimeError("설치 파일 크기 메타데이터가 없거나 비정상입니다")
        if not _valid_sha256(asset.sha256):
            raise RuntimeError("설치 파일 SHA256 메타데이터가 없습니다")

    # ---------------- 2) 받고 검증 ----------------

    def download(self, asset: InstallerAsset,
                 on_progress: Callable[[AppUpdateProgress], None]) -> Path:
        """Setup을 .part에 받은 뒤 모든 검증을 통과해야 최종 경로로 승격한다."""
        self._validate_asset(asset)
        if not self._trusted_thumbprints():
            raise RuntimeError("코드서명 인증서가 설정되지 않아 자동 업데이트를 중단합니다")

        target_dir = app_update_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = CANONICAL_SETUP_NAME_FMT.format(version=asset.version)
        dest = target_dir / filename
        part = dest.with_name(dest.name + ".part")
        self._cleanup(part)

        req = urllib.request.Request(asset.url, headers={"User-Agent": HTTP_UA})
        try:
            with urllib.request.urlopen(req, timeout=self.DOWNLOAD_TIMEOUT) as resp:
                _require_trusted_response_url(
                    resp, asset.url, TRUSTED_UPDATE_HOSTS, "설치 파일 리디렉션")
                raw_length = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
                if raw_length:
                    try:
                        if int(raw_length) != asset.size:
                            raise RuntimeError("설치 파일 응답 크기가 릴리스 정보와 다릅니다")
                    except ValueError as exc:
                        raise RuntimeError("설치 파일 응답 크기가 올바르지 않습니다") from exc
                done = 0
                last = 0.0
                with open(part, "xb") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if done > asset.size or done > self.MAX_EXE_BYTES:
                            raise RuntimeError("받는 파일 크기가 릴리스 정보와 다릅니다")
                        now = time.monotonic()
                        if now - last >= self.PROGRESS_INTERVAL or done == asset.size:
                            last = now
                            on_progress(AppUpdateProgress(
                                stage="downloading",
                                pct=done / asset.size * 100,
                                message=f"{done/1048576:.0f} / {asset.size/1048576:.0f} MB",
                            ))
                    f.flush()
                    os.fsync(f.fileno())
            if done != asset.size:
                raise RuntimeError("받은 파일 크기가 릴리스 정보와 다릅니다")

            on_progress(AppUpdateProgress(stage="verifying", pct=100.0,
                                          message="서명과 무결성 확인 중"))
            self._verify(part, asset)
            self._verify_windows_trust(part, asset.version)
            os.replace(part, dest)
            return dest
        except Exception:
            self._cleanup(part)
            raise

    @classmethod
    def _verify(cls, path: Path, asset: InstallerAsset) -> None:
        """받은 설치본의 크기·SHA256·PE 헤더를 검증한다."""
        size = path.stat().st_size
        if not (cls.MIN_EXE_BYTES <= size <= cls.MAX_EXE_BYTES):
            raise RuntimeError(f"받은 파일 크기가 비정상입니다 ({size/1048576:.1f} MB)")
        if asset.size <= 0 or size != asset.size:
            raise RuntimeError("받은 파일이 릴리스 정보와 크기가 다릅니다 (전송 중 손상)")
        if not _valid_sha256(asset.sha256):
            raise RuntimeError("SHA256 메타데이터가 없어 설치할 수 없습니다")
        if cls._sha256(path) != asset.sha256.strip().lower():
            raise RuntimeError("SHA256 해시가 일치하지 않습니다 (변조 의심)")
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                raise RuntimeError("받은 파일이 실행 파일이 아닙니다")

    @staticmethod
    def _trusted_thumbprints() -> frozenset[str]:
        values = frozenset(re.sub(r"\s+", "", value).upper()
                           for value in TRUSTED_SIGNER_THUMBPRINTS
                           if isinstance(value, str))
        if any(re.fullmatch(r"[0-9A-F]{40}", value) is None for value in values):
            raise RuntimeError("신뢰할 코드서명 인증서 thumbprint 설정이 잘못되었습니다")
        return values

    @classmethod
    def _verify_windows_trust(cls, path: Path, expected_version: str) -> None:
        """Authenticode 상태·서명자 thumbprint·PE ProductVersion을 확인한다."""
        trusted = cls._trusted_thumbprints()
        if not trusted:
            raise RuntimeError("코드서명 인증서가 설정되지 않아 설치를 차단했습니다")
        if os.name != "nt":
            raise RuntimeError("Authenticode 검증은 Windows에서만 수행할 수 있습니다")
        version = _normalize_release_version(expected_version)
        script = (
            "$ErrorActionPreference='Stop';"
            "$p=$env:SSAK_SETUP_PATH;"
            "$s=Get-AuthenticodeSignature -LiteralPath $p;"
            "$f=[Diagnostics.FileVersionInfo]::GetVersionInfo($p);"
            "$thumb='';if($s.SignerCertificate){$thumb=$s.SignerCertificate.Thumbprint};"
            "[ordered]@{Status=[string]$s.Status;Thumbprint=$thumb;"
            "ProductVersion=[string]$f.ProductVersion}|ConvertTo-Json -Compress"
        )
        # Resolve the verifier from Windows itself; PATH/current-directory lookup
        # could execute a different powershell.exe and forge a successful trust result.
        import ctypes
        system_dir = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(system_dir, len(system_dir))
        if length <= 0 or length >= len(system_dir):
            raise RuntimeError("Windows 시스템 경로를 확인하지 못했습니다")
        powershell = Path(system_dir.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not powershell.is_absolute() or not powershell.is_file() or powershell.is_symlink():
            raise RuntimeError("Windows 시스템 서명 검사기를 찾지 못했습니다")
        env = os.environ.copy()
        env["SSAK_SETUP_PATH"] = str(path.resolve())
        try:
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=25, check=False, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("설치 파일 코드서명을 확인하지 못했습니다") from exc
        if result.returncode != 0:
            raise RuntimeError("설치 파일 코드서명 검증 명령이 실패했습니다")
        try:
            info = json.loads(result.stdout.strip())
        except (ValueError, TypeError) as exc:
            raise RuntimeError("설치 파일 코드서명 결과를 해석하지 못했습니다") from exc
        status = str(info.get("Status") or "") if isinstance(info, dict) else ""
        thumbprint = re.sub(r"\s+", "", str(info.get("Thumbprint") or "")).upper() \
            if isinstance(info, dict) else ""
        product_version = str(info.get("ProductVersion") or "").strip() \
            if isinstance(info, dict) else ""
        if status != "Valid":
            raise RuntimeError(f"설치 파일의 Authenticode 서명이 유효하지 않습니다 ({status or '없음'})")
        if thumbprint not in trusted:
            raise RuntimeError("설치 파일 서명자가 신뢰 목록과 다릅니다")
        if _RELEASE_VERSION_RE.fullmatch(product_version) is None:
            raise RuntimeError("설치 파일 ProductVersion이 올바르지 않습니다")
        if compare_versions(product_version, version) != 0:
            raise RuntimeError("설치 파일 ProductVersion이 요청한 버전과 다릅니다")

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest().lower()

    # ---------------- 3) 설치 시작 ----------------

    def launch(self, setup: Path, expected_version: str) -> None:
        """신뢰 검증을 다시 수행한 뒤 설치본을 무인 모드로 띄운다.

        다운로드 검증과 실행 사이 파일 교체(TOCTOU)를 막기 위해 실행 직전에
        해시 외 Authenticode/버전 검증을 한 번 더 수행한다."""
        self._verify_windows_trust(setup, expected_version)
        subprocess.Popen(
            [str(setup)] + self.SETUP_ARGS,
            close_fds=True,
            creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                           | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        )
        log.info("app update: installer launched (%s)", setup)

    @staticmethod
    def _cleanup(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass


# ============================================================
#  서비스: ffmpeg 자동 설치
# ============================================================

@dataclass
class FfmpegProgress:
    stage: str        # "downloading" | "extracting" | "done" | "error"
    pct: float = 0.0
    message: str = ""


@dataclass(frozen=True)
class FfmpegAsset:
    url: str
    size: int
    sha256: str
    executable_size: int
    executable_sha256: str


class FfmpegInstaller:
    """독립 검토한 immutable BtbN archive에서 ffmpeg.exe를 설치한다.

    release tag/URL, ZIP size/SHA-256, 추출 EXE size/SHA-256이 소스에 모두
    고정된 경우에만 앱 소유 cache를 사용한다. GitHub의 mutable ``latest`` API나
    로컬 PATH 바이너리는 신뢰하지 않으며, hash 검증 뒤에만 smoke test를 실행한다.
    """

    GITHUB_REPO = "BtbN/FFmpeg-Builds"
    RELEASE_TAG = FFMPEG_RELEASE_TAG
    ASSET_NAME = FFMPEG_ASSET_NAME
    URL = FFMPEG_ARCHIVE_URL
    EXPECTED_SHA256 = FFMPEG_ARCHIVE_SHA256
    EXPECTED_SIZE = FFMPEG_ARCHIVE_SIZE
    EXPECTED_EXE_SHA256 = FFMPEG_EXECUTABLE_SHA256
    EXPECTED_EXE_SIZE = FFMPEG_EXECUTABLE_SIZE

    DOWNLOAD_TIMEOUT = 60
    MIN_ZIP_BYTES = 5 * 1024 * 1024
    MAX_ZIP_BYTES = 500 * 1024 * 1024
    MIN_EXE_BYTES = 1 * 1024 * 1024
    MAX_EXE_BYTES = 300 * 1024 * 1024
    MAX_ZIP_MEMBERS = 10_000
    MAX_ZIP_UNCOMPRESSED = 2 * 1024 * 1024 * 1024

    def __init__(self) -> None:
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise RuntimeError("ffmpeg 설치가 취소되었습니다")

    def install(self, on_progress: Callable[[FfmpegProgress], None]) -> bool:
        target_dir = ffmpeg_cache_dir()
        target = target_dir / "ffmpeg.exe"
        tmp_zip: Path | None = None
        tmp_exe: Path | None = None
        try:
            # Pin 검증이 cache 검사보다 먼저다. 운영자가 pin을 채우지 않은 빌드는
            # 과거 cache도 실행하지 않고 즉시 fail-closed한다.
            asset = self._resolve_asset()
            target_dir.mkdir(parents=True, exist_ok=True)
            if target.is_file() and not target.is_symlink():
                try:
                    self._verify_executable(
                        target, asset.executable_sha256, asset.executable_size)
                    if self._smoke_test(target):
                        on_progress(FfmpegProgress(
                            stage="done", pct=100.0, message="이미 설치됨"))
                        return True
                    raise RuntimeError("ffmpeg 실행 확인에 실패했습니다")
                except (OSError, RuntimeError):
                    # 앱 소유 cache이지만 현재 release pin과 다르면 실행하지 않고
                    # 아래의 검증 다운로드로 교체한다.
                    log.warning("cached ffmpeg rejected", exc_info=True)

            zip_fd, zip_name = tempfile.mkstemp(
                prefix="ffmpeg-", suffix=".zip.part", dir=str(target_dir.parent))
            os.close(zip_fd)
            tmp_zip = Path(zip_name)
            exe_fd, exe_name = tempfile.mkstemp(
                prefix="ffmpeg-", suffix=".part.exe", dir=str(target_dir))
            os.close(exe_fd)
            tmp_exe = Path(exe_name)

            self._check_cancelled()
            self._download(asset, tmp_zip, on_progress)
            self._check_cancelled()
            on_progress(FfmpegProgress(stage="extracting", message="무결성 검증 중..."))
            self._verify_zip(
                tmp_zip, asset.sha256, asset.size, asset.executable_size)
            self._check_cancelled()
            on_progress(FfmpegProgress(stage="extracting", message="압축 해제 중..."))
            self._extract(tmp_zip, tmp_exe)
            self._check_cancelled()
            self._verify_executable(
                tmp_exe, asset.executable_sha256, asset.executable_size)
            if not self._smoke_test(tmp_exe):
                raise RuntimeError("ffmpeg 실행 확인에 실패했습니다")
            os.replace(tmp_exe, target)
            tmp_exe = None
        except Exception as e:
            log.exception("ffmpeg install failed")
            on_progress(FfmpegProgress(stage="error", message=f"설치 실패: {e}"))
            return False
        finally:
            if tmp_zip is not None:
                self._cleanup(tmp_zip)
            if tmp_exe is not None:
                self._cleanup(tmp_exe)

        on_progress(FfmpegProgress(stage="done", pct=100.0, message="설치 완료"))
        return True

    def _download(self, asset: FfmpegAsset, dest: Path,
                  on_progress: Callable[[FfmpegProgress], None]) -> None:
        url = _require_trusted_https_url(
            asset.url, TRUSTED_UPDATE_HOSTS, "ffmpeg 다운로드 주소")
        req = urllib.request.Request(url, headers={"User-Agent": HTTP_UA})
        with urllib.request.urlopen(req, timeout=self.DOWNLOAD_TIMEOUT) as resp:
            _require_trusted_response_url(
                resp, url, TRUSTED_UPDATE_HOSTS, "ffmpeg 다운로드 리디렉션")
            raw_length = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
            if raw_length:
                try:
                    if int(raw_length) != asset.size:
                        raise RuntimeError("ffmpeg 응답 크기가 릴리스 정보와 다릅니다")
                except ValueError as exc:
                    raise RuntimeError("ffmpeg 응답 크기가 올바르지 않습니다") from exc
            done = 0
            with open(dest, "wb") as f:
                while True:
                    self._check_cancelled()
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if done > asset.size or done > self.MAX_ZIP_BYTES:
                        raise RuntimeError("ffmpeg 다운로드 크기가 릴리스 정보와 다릅니다")
                    on_progress(FfmpegProgress(
                        stage="downloading", pct=done / asset.size * 100,
                        message=f"{done/1048576:.1f} / {asset.size/1048576:.1f} MB",
                    ))
                f.flush()
                os.fsync(f.fileno())
        if done != asset.size:
            raise RuntimeError("ffmpeg 다운로드가 완전하지 않습니다")

    @classmethod
    def _resolve_asset(cls) -> FfmpegAsset:
        """소스에 고정된 immutable ZIP/EXE pin을 검증하며 network를 사용하지 않는다."""
        tag = str(cls.RELEASE_TAG or "").strip()
        name = str(cls.ASSET_NAME or "").strip()
        archive_hash = str(cls.EXPECTED_SHA256 or "").strip().lower()
        executable_hash = str(cls.EXPECTED_EXE_SHA256 or "").strip().lower()
        try:
            archive_size = int(cls.EXPECTED_SIZE or 0)
            executable_size = int(cls.EXPECTED_EXE_SIZE or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("ffmpeg size pin이 올바르지 않습니다") from exc

        if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag) is None
                or tag.lower() == "latest"):
            raise RuntimeError("ffmpeg immutable release tag를 설정해야 합니다")
        if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,255}\.zip", name) is None
                or Path(name).name != name):
            raise RuntimeError("ffmpeg archive 파일명 pin이 올바르지 않습니다")
        expected_url = (
            f"https://github.com/{cls.GITHUB_REPO}/releases/download/{tag}/{name}"
        )
        if cls.URL != expected_url:
            raise RuntimeError("ffmpeg archive URL이 immutable release pin과 다릅니다")
        if not _valid_sha256(archive_hash) or not (
                cls.MIN_ZIP_BYTES <= archive_size <= cls.MAX_ZIP_BYTES):
            raise RuntimeError("ffmpeg ZIP size/SHA-256 pin을 모두 설정해야 합니다")
        if not _valid_sha256(executable_hash) or not (
                cls.MIN_EXE_BYTES <= executable_size <= cls.MAX_EXE_BYTES):
            raise RuntimeError("ffmpeg EXE size/SHA-256 pin을 모두 설정해야 합니다")
        _require_trusted_https_url(
            expected_url, TRUSTED_UPDATE_HOSTS, "ffmpeg 다운로드 주소")
        return FfmpegAsset(
            expected_url, archive_size, archive_hash,
            executable_size, executable_hash,
        )

    @classmethod
    def _verify_zip(cls, zip_path: Path, expected_sha256: str | None = None,
                    expected_size: int | None = None,
                    expected_executable_size: int | None = None) -> None:
        """zip 크기·필수 SHA-256·구조·추출 EXE 크기 pin을 검증한다."""
        size = zip_path.stat().st_size
        if not (cls.MIN_ZIP_BYTES <= size <= cls.MAX_ZIP_BYTES):
            raise RuntimeError(
                f"받은 파일 크기가 비정상입니다 ({size/1024/1024:.1f} MB)"
            )
        if expected_size is not None and (expected_size <= 0 or size != expected_size):
            raise RuntimeError("ffmpeg zip 크기가 릴리스 정보와 다릅니다")
        if expected_sha256 is not None:
            if not _valid_sha256(expected_sha256):
                raise RuntimeError("ffmpeg SHA256 메타데이터가 올바르지 않습니다")
            if cls._sha256(zip_path).lower() != expected_sha256.strip().lower():
                raise RuntimeError("SHA256 해시가 일치하지 않습니다 (변조 의심)")

        with zipfile.ZipFile(zip_path) as z:
            infos = z.infolist()
            if len(infos) > cls.MAX_ZIP_MEMBERS:
                raise RuntimeError("ffmpeg zip 항목이 비정상적으로 많습니다")
            if sum(info.file_size for info in infos) > cls.MAX_ZIP_UNCOMPRESSED:
                raise RuntimeError("ffmpeg zip 압축 해제 크기가 너무 큽니다")
            matches = []
            for info in infos:
                name = info.filename.replace("\\", "/")
                parts = [part for part in name.split("/") if part]
                mode = (info.external_attr >> 16) & 0xFFFF
                if (name.startswith("/") or ".." in parts or info.flag_bits & 0x1
                        or stat.S_ISLNK(mode)):
                    raise RuntimeError("ffmpeg zip에 안전하지 않은 항목이 있습니다")
                if name.endswith("/bin/ffmpeg.exe"):
                    matches.append(info)
            if len(matches) != 1:
                raise RuntimeError("zip 구조가 예상과 다릅니다 (ffmpeg.exe 중복/없음)")
            member_size = matches[0].file_size
            if not (cls.MIN_EXE_BYTES <= member_size <= cls.MAX_EXE_BYTES):
                raise RuntimeError("zip 안 ffmpeg.exe 크기가 비정상입니다")
            if (expected_executable_size is not None
                    and (expected_executable_size <= 0
                         or member_size != expected_executable_size)):
                raise RuntimeError("zip 안 ffmpeg.exe 크기가 고정 pin과 다릅니다")

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @classmethod
    def _extract(cls, zip_path: Path, target: Path) -> None:
        with zipfile.ZipFile(zip_path) as z:
            members = [info for info in z.infolist()
                       if info.filename.replace("\\", "/").endswith("/bin/ffmpeg.exe")]
            if len(members) != 1:
                raise RuntimeError("zip 안 ffmpeg.exe가 정확히 하나가 아닙니다")
            info = members[0]
            if not (cls.MIN_EXE_BYTES <= info.file_size <= cls.MAX_EXE_BYTES):
                raise RuntimeError("ffmpeg.exe 크기가 비정상입니다")
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())

    @classmethod
    def _verify_executable(cls, path: Path, expected_sha256: str,
                           expected_size: int) -> None:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("ffmpeg.exe가 일반 파일이 아닙니다")
        size = path.stat().st_size
        if (not cls.MIN_EXE_BYTES <= size <= cls.MAX_EXE_BYTES
                or expected_size <= 0 or size != expected_size):
            raise RuntimeError("추출한 ffmpeg.exe 크기가 고정 pin과 다릅니다")
        if not _valid_sha256(expected_sha256):
            raise RuntimeError("ffmpeg.exe SHA-256 pin이 올바르지 않습니다")
        if cls._sha256(path).lower() != expected_sha256.strip().lower():
            raise RuntimeError("ffmpeg.exe SHA-256이 고정 pin과 다릅니다")
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                raise RuntimeError("추출한 파일이 Windows 실행 파일이 아닙니다")

    @staticmethod
    def _smoke_test(path: Path) -> bool:
        try:
            result = subprocess.run(
                [str(path), "-version"], capture_output=True, text=True,
                timeout=15, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = ((result.stdout or "") + "\n" + (result.stderr or "")).lower()
            return result.returncode == 0 and "ffmpeg version" in output
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _cleanup(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass


# ============================================================
#  서비스: 사용자 설정 영속화
# ============================================================

class Settings:
    """작은 JSON 설정 저장소. process-local writer 직렬화 + unique temp + replace."""

    _locks_guard = threading.Lock()
    _path_locks: dict[str, threading.RLock] = {}

    def __init__(self, path: Path | None = None):
        self.path = path or config_path()
        key = os.path.normcase(os.path.abspath(str(self.path)))
        with self._locks_guard:
            self._write_lock = self._path_locks.setdefault(key, threading.RLock())

    def load(self) -> dict:
        with self._write_lock:
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}

    def save(self, data: dict) -> bool:
        tmp: Path | None = None
        with self._write_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, raw_tmp = tempfile.mkstemp(
                    prefix=self.path.name + ".", suffix=".tmp",
                    dir=str(self.path.parent), text=True,
                )
                tmp = Path(raw_tmp)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(dict(data), f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                tmp = None
                return True
            except (OSError, TypeError, ValueError):
                log.exception("settings save failed")
                return False
            finally:
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        tmp.unlink()


# ============================================================
#  UI (pywebview) — 디자인 HTML 을 렌더하고 백엔드에 연결
# ============================================================

def _ui_html_path() -> str:
    """web_ui/index.html 경로 (소스 실행 / PyInstaller onefile 번들 모두 대응)."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).parent
    return str(base / "web_ui" / "index.html")


def read_clipboard_text() -> str:
    """Windows 클립보드의 텍스트를 ctypes 로 읽는다 (의존성 없음). 실패 시 ''."""
    if os.name != "nt":
        return ""
    import ctypes
    CF_UNICODETEXT = 13
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.GetClipboardData.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u32.OpenClipboard(0):
        return ""
    try:
        if not u32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = u32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.c_wchar_p(ptr).value or ""
        finally:
            k32.GlobalUnlock(handle)
    except Exception:
        return ""
    finally:
        u32.CloseClipboard()


class Api:
    """JS(window.pywebview.api)에서 호출하는 백엔드. 모든 사이드이펙트는 여기 격리."""

    def __init__(self) -> None:
        # ⚠ pywebview 가 js_api 객체의 '언더스코어 아닌' 속성을 재귀적으로 탐색하다
        #   커스텀 객체(window/서비스)로 파고들어 무한재귀/COM 오류를 내고 브리지가
        #   안 붙는다. 그래서 메서드가 아닌 내부 상태는 전부 '_' 접두사로 둬서
        #   pywebview 가 건너뛰게 한다. (JS 에 노출할 것은 메서드뿐.)
        self._window = None
        self._settings = Settings()
        self._cfg = self._settings.load()
        self._analyzer = VideoAnalyzer()
        self._installer = FfmpegInstaller()
        self._updater = UpdateChecker()
        self._app_updater = AppUpdater()
        # 업데이트 체크 결과. 배너의 '지금 업데이트' 가 이걸 보고 설치본을 받는다.
        self._pending_update: UpdateDecision | None = None
        self._updating = False
        self._ffmpeg_dir = find_ffmpeg()
        # 분석 결과와 쿠키 사용 여부는 URL별 단기 컨텍스트로 묶는다. 전역 한 칸을
        # 공유하면 동시 분석 A의 쿠키 상태가 다운로드 B에 섞일 수 있다.
        self._analysis_contexts: dict[str, tuple[float, str, str | None]] = {}
        self._files: dict[str, str] = {}              # job_id → 최종 파일 경로
        self._job_roots: dict[str, str] = {}          # job_id → 해당 작업 저장 root
        self._active: dict[str, Downloader] = {}      # 동시 진행 중 (job_id → Downloader)
        self._workers: dict[str, threading.Thread] = {}
        self._background_threads: set[threading.Thread] = set()
        self._state_lock = threading.RLock()
        self._analysis_condition = threading.Condition(self._state_lock)
        self._analysis_active = 0
        self._analysis_slots = threading.BoundedSemaphore(2)
        self._closing = False
        self._shutdown_complete = threading.Event()
        self._cfg["cookie_opt_in"] = self._cfg.get("cookie_opt_in") is True
        self._maxed = False

    def _set_window(self, w) -> None:
        """내부 초기화용. '_' 메서드는 pywebview JS API에 노출되지 않는다."""
        self._window = w

    # ---------------- 내부 헬퍼 ----------------

    def _js(self, code: str) -> None:
        with self._state_lock:
            if self._closing:
                return
            w = self._window
        if w is None:
            return
        try:
            w.evaluate_js(code)
        except Exception:
            pass

    def _js_call(self, function_name: str, *args) -> None:
        """고정 함수명과 JSON 인자로만 Python→JavaScript 호출을 구성한다.

        값이 JavaScript 소스 코드나 문자열 구분자로 해석되지 않도록 job id를
        포함한 모든 동적 값을 json.dumps로 직렬화한다.
        """
        if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", function_name):
            raise ValueError("unsafe JavaScript function name")
        encoded = ",".join(json.dumps(arg, ensure_ascii=True) for arg in args)
        self._js(f"{function_name}({encoded})")

    @staticmethod
    def _valid_job_id(job_id: object) -> bool:
        return isinstance(job_id, str) and JOB_ID_RE.fullmatch(job_id) is not None

    @staticmethod
    def _valid_choice(value: object, allowed: list[tuple[str, object]]) -> bool:
        return isinstance(value, str) and value in {label for label, _ in allowed}

    @staticmethod
    def _default_dir() -> str:
        return str(Path.home() / "Downloads" / "싹싹김치 다운로드")

    def _save_short(self) -> str:
        with self._state_lock:
            full = str(self._cfg.get("save_dir", self._default_dir()))
        parts = [p for p in re.split(r"[\\/]", full) if p]
        return "…\\" + "\\".join(parts[-2:]) if len(parts) > 2 else full

    def _update_settings(self, **patch) -> bool:
        """candidate를 먼저 저장하고 성공한 경우에만 메모리 상태를 commit한다."""
        with self._state_lock:
            candidate = dict(self._cfg)
            candidate.update(patch)
            if not self._settings.save(candidate):
                return False
            self._cfg = candidate
            return True

    def _cookie_opted_in(self) -> bool:
        with self._state_lock:
            return self._cfg.get("cookie_opt_in") is True

    def _start_background(self, name: str, target: Callable, *args) -> bool:
        """종료 시 join할 수 있도록 daemon 작업을 공통 registry에 등록한다."""
        def runner() -> None:
            try:
                target(*args)
            finally:
                with self._state_lock:
                    self._background_threads.discard(threading.current_thread())

        thread = threading.Thread(target=runner, name=name, daemon=True)
        with self._state_lock:
            if self._closing:
                return False
            self._background_threads.add(thread)
            try:
                thread.start()
            except RuntimeError:
                self._background_threads.discard(thread)
                raise
        return True

    def _run_download_worker(self, job_id: str, dl: Downloader,
                             cfg: DownloadConfig) -> None:
        try:
            dl.run(cfg)
        finally:
            with self._state_lock:
                self._active.pop(job_id, None)
                self._workers.pop(job_id, None)
                if job_id not in self._files:
                    self._job_roots.pop(job_id, None)
                no_active = not self._active
            if no_active:
                self._js("setBusy(false)")

    def _safe_job_file(self, job_id: str) -> Path | None:
        with self._state_lock:
            raw_path = self._files.get(job_id)
            raw_root = self._job_roots.get(job_id)
        if not raw_path or not raw_root:
            return None
        try:
            original = Path(raw_path)
            if original.is_symlink():
                return None
            path = original.resolve(strict=True)
            root = Path(raw_root).resolve(strict=True)
            path.relative_to(root)
            if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
                return None
            return path
        except (OSError, ValueError):
            return None

    def _shutdown(self, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """신규 작업을 닫고 다운로드를 취소한 뒤 bounded join한다 (idempotent)."""
        with self._state_lock:
            if self._shutdown_complete.is_set():
                return
            if self._closing:
                already_closing = True
            else:
                self._closing = True
                already_closing = False
                active = list(self._active.items())
                workers = dict(self._workers)
                background = list(self._background_threads)
        if already_closing:
            self._shutdown_complete.wait(timeout=max(0.0, timeout))
            return

        for _, dl in active:
            # 종료 중에는 부분/완성 산출물을 지우지 않아 재실행 전 수동 복구가 가능하다.
            dl._preserve_tmpdir = True
            dl.cancel()
        self._installer.cancel()
        deadline = time.monotonic() + max(0.0, timeout)
        current = threading.current_thread()
        for thread in [*workers.values(), *background]:
            if thread is current or not thread.is_alive():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

        with self._analysis_condition:
            while self._analysis_active and time.monotonic() < deadline:
                self._analysis_condition.wait(timeout=max(0.0, deadline - time.monotonic()))

        alive_ids = [job_id for job_id, thread in workers.items() if thread.is_alive()]
        if alive_ids:
            # 강제 프로세스 종료로 partial 작업이 사라지지 않도록 폴더 보존 플래그.
            for job_id in alive_ids:
                dl = dict(active).get(job_id)
                if dl is not None:
                    dl._preserve_tmpdir = True
            log.warning("shutdown timeout; preserving %d download work folders", len(alive_ids))
        POT.stop()
        self._shutdown_complete.set()

    def _qualities_for(self, result: AnalysisResult) -> list[str]:
        if not result.heights:
            return [lbl for lbl, _ in QUALITY_TABLE]
        max_h = max(result.heights)
        return [lbl for lbl, h in QUALITY_TABLE if h is None or h == -1 or h <= max_h]

    def _probe_auto(self, url: str) -> tuple[AnalysisResult, str | None]:
        try:
            return self._analyzer.probe(url, None), None
        except Exception as e:
            if not self._cookie_opted_in() or not looks_like_login_error(str(e)):
                raise
            browser = detect_cookie_browser()
            if not browser:
                raise
            log.info("retrying analysis with explicitly enabled %s cookies", browser)
            return self._analyzer.probe(url, browser), browser

    def _remember_analysis(self, url: str, browser: str | None) -> str:
        now = time.monotonic()
        token = secrets.token_urlsafe(24)
        with self._state_lock:
            if self._closing:
                return ""
            self._analysis_contexts = {
                key: value for key, value in self._analysis_contexts.items()
                if now - value[0] <= ANALYSIS_TTL_SECONDS
            }
            while len(self._analysis_contexts) >= MAX_ANALYSIS_CONTEXTS:
                oldest = min(self._analysis_contexts,
                             key=lambda key: self._analysis_contexts[key][0])
                self._analysis_contexts.pop(oldest, None)
            self._analysis_contexts[token] = (now, url, browser)
        return token

    def _consume_analysis(self, token: object, url: str) -> tuple[bool, str | None]:
        if not isinstance(token, str) or len(token) > 128:
            return False, None
        with self._state_lock:
            value = self._analysis_contexts.pop(token, None)
        if value is None:
            return False, None
        created, analyzed_url, browser = value
        if time.monotonic() - created > ANALYSIS_TTL_SECONDS or analyzed_url != url:
            return False, None
        return True, browser

    # ---------------- JS 호출용 API ----------------

    def get_init(self) -> dict:
        log.info("bridge ready: get_init called")
        with self._state_lock:
            cfg = dict(self._cfg)
        return {
            "app_title": APP_TITLE,
            "version": APP_VERSION,
            "ffmpeg_ok": self._ffmpeg_dir is not None,
            "tos_accepted": cfg.get("tos_accepted") == TOS_VERSION,
            "save_dir": cfg.get("save_dir", self._default_dir()),
            "quality": cfg.get("quality", "1080p (FHD)"),
            "format": cfg.get("format", FORMAT_TABLE[0][0]),
            "cookie_opt_in": cfg.get("cookie_opt_in") is True,
            "qualities": [lbl for lbl, _ in QUALITY_TABLE],
            "formats": [lbl for lbl, _ in FORMAT_TABLE],
            "promo_url": PROMO_URL,
            "hint": "영상 주소를 복사한 뒤 '링크 붙여넣기'를 누르면 화질·포맷 선택 팝업이 뜹니다.",
        }

    def accept_tos(self) -> None:
        self._update_settings(tos_accepted=TOS_VERSION)

    def set_cookie_opt_in(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            return {"ok": False, "error": "쿠키 설정 값이 올바르지 않습니다",
                    "enabled": self._cookie_opted_in()}
        if not self._update_settings(cookie_opt_in=enabled):
            return {"ok": False, "error": "설정을 저장하지 못했습니다",
                    "enabled": self._cookie_opted_in()}
        if not enabled:
            with self._state_lock:
                self._analysis_contexts.clear()
        return {"ok": True, "enabled": self._cookie_opted_in()}

    def read_clipboard(self) -> str:
        return read_clipboard_text()

    def analyze(self, url: str) -> dict:
        if not isinstance(url, str):
            return {"ok": False, "error": "올바른 공개 링크(http/https)가 아닙니다"}
        url = url.strip()
        try:
            validate_public_http_url(url)
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        if not self._analysis_slots.acquire(blocking=False):
            return {"ok": False, "error": "분석 작업이 많습니다. 잠시 후 다시 시도해 주세요"}
        with self._analysis_condition:
            if self._closing:
                self._analysis_slots.release()
                return {"ok": False, "error": "앱을 종료하는 중입니다"}
            self._analysis_active += 1
        try:
            result, browser = self._probe_auto(url)
        except Exception as e:
            safe_error = redact_urls_in_text(e)
            log.info("analyze failed: %s", safe_error)
            return {"ok": False,
                    "error": (safe_error.splitlines()[-1] if safe_error else "분석 실패")[:140]}
        finally:
            with self._analysis_condition:
                self._analysis_active -= 1
                self._analysis_condition.notify_all()
            self._analysis_slots.release()
        analysis_id = self._remember_analysis(url, browser)
        if not analysis_id:
            return {"ok": False, "error": "앱을 종료하는 중입니다"}
        qs = self._qualities_for(result)
        with self._state_lock:
            default_q = self._cfg.get("quality", "1080p (FHD)")
        if default_q not in qs:
            default_q = "1080p (FHD)" if "1080p (FHD)" in qs else qs[0]
        return {
            "ok": True, "url": url, "analysis_id": analysis_id,
            "title": result.title, "meta": result.meta,
            "thumbnail": result.thumbnail, "qualities": qs, "quality": default_q,
            "save_short": self._save_short(),
        }

    def start_download(self, job_id: str, url: str, quality: str, fmt: str,
                       analysis_id: str = "") -> dict:
        if not self._valid_job_id(job_id):
            return {"ok": False, "error": "잘못된 작업 식별자입니다"}
        if not isinstance(url, str):
            return {"ok": False, "error": "올바른 공개 링크(http/https)가 아닙니다"}
        url = url.strip()
        try:
            validate_public_http_url(url)
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        if not self._valid_choice(quality, QUALITY_TABLE):
            return {"ok": False, "error": "지원하지 않는 화질 선택입니다"}
        if not self._valid_choice(fmt, FORMAT_TABLE):
            return {"ok": False, "error": "지원하지 않는 파일 형식입니다"}
        with self._state_lock:
            if self._closing:
                return {"ok": False, "error": "앱을 종료하는 중입니다"}
            if job_id in self._active or job_id in self._files:
                return {"ok": False, "error": "이미 사용 중인 작업 식별자입니다"}
            if len(self._active) >= MAX_ACTIVE_DOWNLOADS:
                return {"ok": False,
                        "error": f"동시에 최대 {MAX_ACTIVE_DOWNLOADS}개까지 받을 수 있습니다"}
            ffmpeg_dir = self._ffmpeg_dir
            save_value = self._cfg.get("save_dir", self._default_dir())
        is_audio = "음성만" in quality
        if ffmpeg_dir is None:
            return {"ok": False, "error": "ffmpeg 준비 중입니다. 잠시 후 다시 시도하세요"}
        save_dir = Path(str(save_value))
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            save_dir = save_dir.resolve(strict=True)
        except OSError as e:
            return {"ok": False, "error": f"저장 폴더를 만들 수 없습니다: {e}"}
        context_ok, cookie_browser = self._consume_analysis(analysis_id, url)
        if not context_ok:
            return {"ok": False, "error": "분석 정보가 만료되었습니다. 링크를 다시 분석해 주세요"}
        if not self._cookie_opted_in():
            cookie_browser = None
        if not self._update_settings(quality=quality, format=fmt):
            return {"ok": False, "error": "설정을 저장하지 못했습니다"}

        height = dict(QUALITY_TABLE).get(quality)
        container = dict(FORMAT_TABLE).get(fmt, "mp4")
        fmt_str = "bestaudio/best" if is_audio else build_format_selector(container)
        cfg = DownloadConfig(
            url=url, out_dir=str(save_dir), format_str=fmt_str,
            is_audio_only=is_audio, container=container, ffmpeg_dir=ffmpeg_dir,
            cookies_browser=cookie_browser, embed_metadata=True,
            use_playwright=False,
            target_height=None if is_audio else height,
        )
        dl = Downloader(
            on_progress=lambda d, j=job_id: self._on_progress(j, d),
            on_done=lambda ok, e, j=job_id: self._on_done(j, ok, e),
            on_item_done=lambda vid, fp=None, dims=None, j=job_id:
                self._on_item_done(j, vid, fp, dims),
            on_status=lambda t, j=job_id: self._on_status(j, t),
        )
        worker = threading.Thread(
            target=self._run_download_worker, args=(job_id, dl, cfg),
            name=f"download-{job_id}", daemon=True)
        with self._state_lock:
            if self._closing:
                return {"ok": False, "error": "앱을 종료하는 중입니다"}
            if job_id in self._active or len(self._active) >= MAX_ACTIVE_DOWNLOADS:
                return {"ok": False, "error": "다운로드 작업 수 제한을 초과했습니다"}
            self._active[job_id] = dl
            self._workers[job_id] = worker
            self._job_roots[job_id] = str(save_dir)
            active_count = len(self._active)
            self._js("setBusy(true)")
            try:
                worker.start()
            except RuntimeError as e:
                self._active.pop(job_id, None)
                self._workers.pop(job_id, None)
                self._job_roots.pop(job_id, None)
                self._js("setBusy(false)")
                log.exception("download worker start failed")
                return {"ok": False, "error": f"다운로드 작업을 시작하지 못했습니다: {e}"}
        log.info("download start id=%s url=%s (active=%d)",
                 job_id, safe_url_for_log(url), active_count)
        return {"ok": True, "job_id": job_id}

    def cancel_download(self) -> None:
        # 진행 중인 모든 다운로드를 취소한다(전체 취소 버튼).
        with self._state_lock:
            active = list(self._active.values())
        for dl in active:
            dl.cancel()

    def forget_item(self, job_id: str) -> None:
        """목록에서 항목 제거(X): 진행 중이면 취소(끝나면 _on_done 이 정리)."""
        if not self._valid_job_id(job_id):
            return
        with self._state_lock:
            dl = self._active.get(job_id)
            self._files.pop(job_id, None)
            self._job_roots.pop(job_id, None)
            no_active = not self._active
        if dl:
            dl.cancel()
        if no_active:
            self._js("setBusy(false)")

    def pick_folder(self) -> str | None:
        try:
            res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            return None
        if not res:
            return None
        d = res[0] if isinstance(res, (list, tuple)) else res
        if not isinstance(d, str) or not d:
            return None
        if not self._update_settings(save_dir=d):
            return None
        return d

    def open_folder(self) -> None:
        with self._state_lock:
            raw = str(self._cfg.get("save_dir", self._default_dir()))
        try:
            folder = Path(raw)
            folder.mkdir(parents=True, exist_ok=True)
            folder = folder.resolve(strict=True)
            if not folder.is_dir():
                raise OSError("저장 경로가 폴더가 아닙니다")
            os.startfile(str(folder))  # type: ignore[attr-defined]  # noqa: S606
        except (OSError, AttributeError):
            log.exception("open folder failed")

    def open_item(self, job_id: str) -> None:
        if not self._valid_job_id(job_id):
            return
        path = self._safe_job_file(job_id)
        if path is None:
            self.open_folder()
            return
        try:
            windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
            explorer = (windir / "explorer.exe").resolve(strict=True)
            subprocess.run(
                [str(explorer), f"/select,{os.path.normpath(str(path))}"],
                shell=False, check=False, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            if path.parent.is_dir():
                os.startfile(str(path.parent))  # type: ignore[attr-defined]  # noqa: S606
        except (OSError, AttributeError):
            self.open_folder()

    def play_item(self, job_id: str) -> None:
        if not self._valid_job_id(job_id):
            return
        path = self._safe_job_file(job_id)
        if path is None:
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
        except (OSError, AttributeError):
            log.info("media open failed: %s", path.name)

    def open_url(self, url: str) -> None:
        safe_open_url(url)

    def start_app_update(self) -> dict:
        """배너의 '지금 업데이트' → 새 설치본을 받아 조용히 재설치한다."""
        with self._state_lock:
            if self._closing:
                return {"ok": False, "error": "앱을 종료하는 중입니다"}
            if self._updating:
                return {"ok": True}
            decision = self._pending_update
            if decision is None:
                return {"ok": False, "error": "업데이트 정보를 찾지 못했습니다"}
            # 설치는 앱을 종료시킨다 → 받는 중인 영상이 있으면 날아간다.
            if self._active:
                return {"ok": False,
                        "error": "다운로드가 끝난 뒤에 업데이트해 주세요"}
            self._updating = True
        try:
            if not self._start_background("app-updater", self._run_app_update, decision):
                with self._state_lock:
                    self._updating = False
                return {"ok": False, "error": "앱을 종료하는 중입니다"}
        except RuntimeError:
            with self._state_lock:
                self._updating = False
            return {"ok": False, "error": "업데이트 작업을 시작하지 못했습니다"}
        return {"ok": True}

    def win_minimize(self) -> None:
        try:
            self._window.minimize()
        except Exception:
            pass

    def win_maximize(self) -> None:
        try:
            if self._maxed:
                self._window.restore()
            else:
                self._window.maximize()
            self._maxed = not self._maxed
        except Exception:
            pass

    def win_close(self) -> None:
        # 유휴 상태는 즉시 종료, 진행 중 다운로드는 JS 확인 후 종료
        self._js("showExit()")

    def exit_confirmed(self) -> None:
        self._shutdown()
        try:
            self._window.destroy()
        except Exception:
            pass

    # ---------------- 다운로더 콜백 (다운로드 스레드 → JS) ----------------

    def _on_progress(self, jid: str, d: dict) -> None:
        if not self._valid_job_id(jid):
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            pct = (done / total * 100) if total else 0
            self._js_call("dlProgress", jid, {
                "pct": round(float(pct), 1),
                "done": round(float(done) / 1048576, 2),
                "total": round(float(total) / 1048576, 2),
                "speed": round(float(speed or 0) / 1048576, 2),
            })
        elif status == "finished":
            self._js_call("dlMerging", jid)

    def _on_status(self, jid: str, text: str) -> None:
        """폴백 재시도/2단계 진입 시 해당 항목에 진행 상태 문구를 보여준다."""
        if self._valid_job_id(jid):
            self._js_call("dlStatus", jid, str(text)[:200])

    def _on_item_done(self, jid: str, item_id: str,
                      filepath: str | None = None,
                      dims: tuple[int, int] | None = None) -> None:
        # postprocessor 훅은 항목마다 여러 번 호출되고 경로도 아직 작업 폴더를
        # 가리킨다. 실제 저장 확정 후 _harvest가 item_id=""로 보낸 콜백만 UI
        # 완료와 재생 경로에 반영한다.
        if item_id or not self._valid_job_id(jid):
            return
        if filepath:
            with self._state_lock:
                self._files[jid] = filepath
            if self._safe_job_file(jid) is None:
                with self._state_lock:
                    self._files.pop(jid, None)
                log.warning("unsafe completed media path rejected")
        with self._state_lock:
            fp = self._files.get(jid, "")
        size = ""
        try:
            if fp:
                size = f"{Path(fp).stat().st_size / 1048576:.1f} MB"
        except OSError:
            pass
        fname = Path(fp).name if fp else ""
        # 실제 저장 해상도를 함께 보낸다 — 선택 화질보다 낮게 나오면 UI가
        # '요청 화질보다 낮음'을 표시해 조용한 저화질 폴백이 없게 한다.
        with self._state_lock:
            dl = self._active.get(jid)
        target = getattr(dl, "_target_height", None) if dl is not None else None
        res = ""
        warn = False
        if dims:
            res = f"{dims[0]}×{dims[1]}"
            warn = isinstance(target, int) and target > 0 and min(dims) < target
            if warn:
                log.warning("선택 %sp 대비 낮은 화질로 저장됨: %s (%sx%s)",
                            target, fname, dims[0], dims[1])
        self._js_call("dlDone", jid, {
            "size": size, "file": fname, "res": res, "warn": warn})

    def _on_done(self, jid: str, ok: bool, err: str | None) -> None:
        if not self._valid_job_id(jid):
            return
        with self._state_lock:
            has_file = jid in self._files
        if ok and not has_file:
            # 성공은 _harvest가 검증된 최종 파일 경로를 등록한 뒤에만 표시한다.
            # 경로 경계 검증 또는 callback이 실패했다면 빈 성공 항목으로 오보고하지 않는다.
            log.error("download committed without a safe final media path: %s", jid)
            self._js_call("dlError", jid, "완료 파일 경로를 안전하게 확인하지 못했습니다")
        elif not ok and err == DOWNLOAD_CANCELLED:
            self._js_call("dlCancelled", jid)
        elif not ok:
            msg = redact_urls_in_text((err or "실패").splitlines()[-1])[:300]
            self._js_call("dlError", jid, msg)

    # ---------------- 백그라운드 (시작 후) ----------------

    def _post_init(self) -> None:
        """WebView 시작 콜백. 종료 시 추적할 background 작업만 시작한다."""
        self._start_background("update-checker", self._kill_and_update)
        if self._ffmpeg_dir is None:
            self._start_background("ffmpeg-installer", self._install_ffmpeg)
        # 클립보드 자동 감지는 두지 않는다 — 사용자가 링크 칸에 '붙여넣을 때만'
        # 팝업이 뜨게 한다(복사만 해도 자동으로 뜨는 동작 제거).

    def _kill_and_update(self) -> None:
        # 독립 서명 manifest가 도입되기 전에는 과거 killswitch 상태를 지우고,
        # unsigned version.json은 새 버전 알림으로만 사용한다.
        with self._state_lock:
            killed = self._cfg.get("killed") is True
        if killed:
            self._update_settings(killed=False, killed_message="")

        decision = self._updater.fetch_decision()
        if decision is None or decision.kind != "new_version":
            return
        with self._state_lock:
            self._pending_update = decision
        self._js_call("updateAvailable", decision.latest_version,
                      decision.download_url or "", decision.release_notes, False)

    def _kill(self, msg: str) -> None:
        self._js_call("alert", str(msg)[:500])
        self.exit_confirmed()

    def _run_app_update(self, decision: UpdateDecision) -> None:
        """설치본 받기 → 검증 → 무인 설치 시작 → 앱 종료.

        어느 단계에서 실패하든 배너를 '브라우저로 받기' 로 되돌려 사용자가
        수동으로 받을 길을 남긴다 (자동이 막혀도 막다른 길이 되지 않도록)."""
        def say(stage: str, pct: float = 0.0, message: str = "") -> None:
            self._js_call("appUpdate", stage, round(float(pct), 1), message)

        say("resolving", 0, "새 버전 확인 중")
        try:
            asset = self._app_updater.resolve(decision)
        except Exception as e:
            log.exception("app update: 설치본을 찾지 못했습니다")
            self._app_update_failed(f"설치 파일을 찾지 못했습니다 ({e})")
            return

        try:
            setup = self._app_updater.download(
                asset, lambda p: say(p.stage, p.pct, p.message))
        except Exception as e:
            log.exception("app update: 다운로드 실패")
            self._app_update_failed(str(e) or "다운로드에 실패했습니다")
            return

        say("launching", 100, "설치를 시작합니다")
        try:
            self._app_updater.launch(setup, decision.latest_version)
        except Exception as e:
            log.exception("app update: 설치 시작 실패")
            self._app_update_failed(f"설치를 시작하지 못했습니다 ({e})")
            return

        # 구버전이 떠 있으면 파일 교체가 막힌다 → 곧바로 종료한다.
        # 설치가 끝나면 설치본이 /AUTORUN=1 로 새 버전을 다시 띄운다.
        time.sleep(0.8)
        log.info("app update: exiting for installer")
        self.exit_confirmed()

    def _app_update_failed(self, msg: str) -> None:
        with self._state_lock:
            self._updating = False
        self._js_call("appUpdate", "error", 0,
                      redact_urls_in_text(str(msg))[:200])

    def _install_ffmpeg(self) -> None:
        def cb(p: FfmpegProgress) -> None:
            if p.stage == "downloading":
                self._js_call("ffmpegStatus", "필요한 파일 받는 중… " + p.message, False)
            elif p.stage == "extracting":
                self._js_call("ffmpegStatus", "설치 중…", False)
            elif p.stage == "done":
                with self._state_lock:
                    self._ffmpeg_dir = find_ffmpeg()
                self._js_call("ffmpegStatus", "준비 완료", True)
            elif p.stage == "error":
                self._js_call("ffmpegStatus", "파일 설치 실패: " + p.message, False)
        self._installer.install(cb)


# ============================================================
#  엔트리포인트
# ============================================================

def main() -> None:
    log_file = setup_logging()
    log.info("starting %s v%s (pywebview), log=%s", APP_TITLE, APP_VERSION, log_file)
    log.info("engine: yt-dlp %s (%s)", yt_dlp.version.__version__,
             _ENGINE_PATH or "exe 번들")
    # POT 공급자 등록은 import 시점(로깅 설정 전)에 끝나서 그때 로그가 사라진다.
    # 유튜브가 안 될 때 제일 먼저 볼 정보라 여기서 한 번 더 남긴다.
    log.info("POT 공급자: %s",
             potprovider_dir() or "없음 — 유튜브 다운로드가 막힐 수 있음")
    # 릴리스 lock으로 번들된 엔진만 사용한다. 독립 publisher 서명 manifest가
    # 생기기 전에는 PyPI runtime 자동 갱신을 시작하지 않는다.
    engine_updater: EngineUpdater | None = None
    engine_thread: threading.Thread | None = None
    if ENGINE_AUTO_UPDATE_ENABLED:
        engine_updater = EngineUpdater(yt_dlp.version.__version__)
        engine_thread = engine_updater.start_async()
    else:
        log.info("engine runtime auto-update disabled; using signed bundled yt-dlp")
    api = Api()
    window = webview.create_window(
        f"{APP_TITLE}",
        _ui_html_path(),
        js_api=api,
        width=860, height=600, min_size=(720, 520),
        frameless=True, easy_drag=False,
        background_color="#ffffff",
    )
    api._set_window(window)
    window.events.loaded += lambda: log.info("WebView UI ready")
    try:
        icon_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        webview.start(api._post_init, gui="edgechromium", icon=str(icon_root / "app.ico"))
    except Exception:
        log.exception("webview start failed")
        raise
    finally:
        if engine_updater is not None:
            engine_updater.cancel()
        api._shutdown()
        if (engine_thread is not None and engine_thread.is_alive()
                and engine_thread is not threading.current_thread()):
            engine_thread.join(timeout=5)
        if engine_thread is not None and engine_thread.is_alive():
            log.warning("engine updater did not stop before shutdown deadline")
        POT.stop()


if __name__ == "__main__":
    main()
