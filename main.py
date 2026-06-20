# -*- coding: utf-8 -*-
"""싹싹김치 다운로드 - 유튜브·인스타·비메오·틱톡 등 1800+ 사이트 지원 GUI

구조
----
- 상수/유틸     : 앱 메타데이터, 디자인 토큰, 순수 함수
- 서비스 계층  : VideoAnalyzer / Downloader / UpdateChecker (UI 비의존, 테스트 가능)
- UI 계층      : DownloaderApp (Tk root + 카드 단위 _build_xxx + 이벤트 핸들러)

사이드 이펙트는 서비스 클래스에 격리하고, UI는 콜백을 통해 결과만 받는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import webview
import yt_dlp


# ============================================================
#  메타데이터 / 설정
# ============================================================

APP_TITLE = "싹싹김치 다운로드"
APP_VERSION = "1.4"
# APP_ID 는 로그/ffmpeg 캐시/config.json 폴더 경로(%LOCALAPPDATA%\<APP_ID>)에
# 쓰인다. 브랜드 식별자라 한 번 정하면 유지한다(바꾸면 기존 사용자 데이터 고아).
APP_ID = "SsakSsakKimchiDownloader"

# HTTP User-Agent 는 반드시 ASCII 여야 한다(헤더는 latin-1). 한글 APP_TITLE 을
# 넣으면 UnicodeEncodeError 로 모든 요청이 실패하므로 ASCII APP_ID 를 쓴다.
HTTP_UA = f"{APP_ID}/{APP_VERSION}"

# 첫 실행 시 받을 이용약관/면책 동의 버전. 약관을 바꾸면 올려 재동의 받는다.
TOS_VERSION = "1"

# 제휴 광고 배너 — 외부 광고주처럼 노출된다 (앱 소유와 무관한 일반 광고 슬롯).
# ※ 아래 도메인을 실제 홍보 대상 홈페이지 주소로 교체하세요.
#    UTM 파라미터로 소유관계 노출 없이 유입량만 측정할 수 있다.
PROMO_URL = "https://www.offcut.dev/?utm_source=ssakssak_downloader&utm_medium=app_banner"
PROMO_TEXT = "광고 ｜ OFFCUT STUDIO — 영상 제작 올인원 워크스페이스 · 자세히 보기 →"
# 광고 배너 이미지 URL. 원격이라 재빌드 없이 소재 교체 가능 — repo 에 올리면
# 자동 반영(없으면 텍스트 배너로 폴백).
# 1) 하단 띠배너 banner.png — 권장 728×90 (더 크게는 728×150 까지)
PROMO_IMAGE_URL = "https://raw.githubusercontent.com/gyqls051-arch/ssakssak-downloader/main/banner.png"
PROMO_IMAGE_MAXW = 700   # 하단 띠배너 최대 폭
PROMO_IMAGE_MAXH = 150   # 하단 띠배너 최대 높이 (이 박스 안에 비율 유지로 맞춤)
# 2) 종료 시 크게 띄울 배너 exit_banner.png — 권장 600×450 (레티나 2배 1200×900)
PROMO_EXIT_IMAGE_URL = "https://raw.githubusercontent.com/gyqls051-arch/ssakssak-downloader/main/exit_banner.png"
PROMO_EXIT_MAXW = 600
PROMO_EXIT_MAXH = 460

# 첫 실행 면책 고지 본문.
TOS_BODY = (
    "본 프로그램은 사용자가 접근 권한을 가진 영상을 개인적으로 저장하기 위한 "
    "도구입니다. 사용 전 아래 내용에 동의해 주세요.\n\n"
    "1. 저작권 준수 책임은 전적으로 사용자에게 있습니다. 저작권자의 허락 없이 "
    "보호되는 콘텐츠를 다운로드·복제·배포하는 행위는 법에 저촉될 수 있으며, "
    "그에 따른 모든 법적 책임은 사용자 본인이 부담합니다.\n\n"
    "2. 사용자는 YouTube 등 각 사이트의 이용약관을 준수해야 합니다.\n\n"
    "3. 본 프로그램은 DRM(기술적 보호조치)이 적용된 콘텐츠는 다운로드하지 "
    "않습니다.\n\n"
    "4. 본 프로그램은 '있는 그대로(AS-IS)' 제공되며, 사용으로 인해 발생하는 "
    "어떠한 직간접적 손해에 대해서도 제작자는 책임지지 않습니다.\n\n"
    "동의하시면 '동의함'을, 동의하지 않으시면 '종료'를 눌러주세요."
)

# 업데이트 채널. 브랜드 격리를 위해 OFFCUT 과 무관한 별도(가능하면 별도 계정/조직)
# 공개 저장소에 version.json 을 올리고 아래 <your-account> 자리를 채우세요.
# <your-account> 가 남아 있으면 업데이트 체크를 건너뜁니다(=배포 전 안전 기본값).
VERSION_INFO_URL = "https://raw.githubusercontent.com/gyqls051-arch/ssakssak-downloader/main/version.json"
VERSION_CHECK_TIMEOUT = 3.0   # 초
VERSION_JSON_MAX_BYTES = 10_000  # 응답 크기 제한 (메모리 보호)

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

# 자동 영상 분석 / 클립보드 자동 감지를 트리거할 대표 동영상 사이트.
# (yt-dlp 는 이 목록과 무관하게 더 많은 사이트를 지원한다. 이 목록은 '자동'
#  동작을 켤지 판단하는 용도일 뿐, 여기 없는 사이트도 수동 다운로드는 된다.)
VIDEO_HOST_RE = re.compile(
    r"(youtube\.com|youtu\.be|instagram\.com|vimeo\.com|tiktok\.com|"
    r"twitter\.com|x\.com|facebook\.com|fb\.watch|fb\.com|twitch\.tv|"
    r"dailymotion\.com|reddit\.com|bilibili\.com|tv\.naver\.com|"
    r"tv\.kakao\.com|soundcloud\.com|streamable\.com)",
    re.IGNORECASE,
)

# ── 성공률 보강용 상수 ──────────────────────────────────────
# 일부 사이트는 우리 앱 UA(HTTP_UA)나 yt-dlp 기본 UA 를 막는다. 다운로드가
# 실패하면 흔한 데스크톱 브라우저 UA + Referer/Origin 헤더로 한 번 더 시도한다.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 멈춘 연결에 무한 대기하지 않게 하는 네트워크 타임아웃(초).
SOCKET_TIMEOUT = 30

# DRM(기술적 보호조치) 보호 콘텐츠로 보이는 오류 키워드. 감지되면 우회를
# 절대 시도하지 않고(불법) 즉시 명확히 안내한 뒤 중단한다.
DRM_HINTS = ("drm", "widevine", "playready", "fairplay")

# 2단계 폴백: yt-dlp 가 사이트를 인식 못 할 때, 페이지 HTML 에서 직접 찾을
# 스트리밍 매니페스트(HLS .m3u8 / DASH .mpd) 패턴.
MANIFEST_RE = re.compile(
    r"""https?://[^\s"'<>\\]+?\.(?:m3u8|mpd)(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)

# Playwright(2단계 최후폴백)는 '소스로 직접 실행할 때'(=개발자/개인)에만 켜진다.
# PyInstaller 로 빌드한 배포 exe(frozen)에서는 항상 OFF → 배포본엔 #7 이 빠지고
# 토글도 없다. (배포 exe 는 build.bat 의 --exclude-module playwright 로 코드까지
# 아예 제외된다.) 개인이 소스로 쓰려면 별도 설치 필요:
#   pip install playwright && playwright install chromium
# (설치 안 해도 프로그램은 정상 동작 — 이 폴백만 자동으로 건너뜀)
ENABLE_PLAYWRIGHT = not getattr(sys, "frozen", False)


# ============================================================
#  디자인 토큰
# ============================================================

# sv-ttk가 깔리면 Win11 네이티브 룩을 입히고, 없으면 clam 폴백.
# 토큰은 라이트 테마 기준 보조 색상 (상태/링크 등).
COLOR_ACCENT = "#0078d4"        # Win11 액센트
COLOR_DANGER = "#c42b1c"
COLOR_SUCCESS = "#0f7b0f"
COLOR_MUTED = "#5c5c5c"
COLOR_LOG_BG = "#fafafa"
COLOR_LOG_BORDER = "#e1e1e1"

# 싹싹김치 디자인 팔레트 (화이트톤 + 블루 액센트). tkinter 라 그라데이션/둥근
# 모서리는 단색·근사치로 표현한다.
DC_BG        = "#eef0f3"   # 창 배경
DC_CARD      = "#fcfcfd"   # 카드 배경
DC_CARD_BD   = "#e7e8ec"   # 카드 테두리
DC_INK       = "#1d2129"   # 본문 글자
DC_MUTED     = "#8a919c"   # 보조 글자(라벨)
DC_MUTED2    = "#9298a2"   # 보조 글자(힌트)
DC_BLUE      = "#2950d8"   # 액센트(프라이머리)
DC_BLUE_DK   = "#2342c4"
DC_SUCCESS   = "#1a7f37"
DC_STATUS_BG = "#edeef1"   # 상태바 배경
DC_BANNER_BG = "#28324c"   # 광고 배너 배경(그라데이션 대신 단색)
DC_BANNER_SUB = "#b9c0d2"  # 배너 보조 글자
DC_BANNER_CTA = "#ff5a2e"  # 배너 CTA 오렌지


# ============================================================
#  로깅
# ============================================================

def setup_logging() -> Path:
    """파일 로그 설정. %LOCALAPPDATA%/<APP_ID>/app.log 에 회전 기록.
    onefile exe도 종료 후 로그가 디스크에 남는다."""
    log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_ID
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=512_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    return log_file


log = logging.getLogger("ssakssak")


# ============================================================
#  순수 유틸
# ============================================================

def build_format_selector(height: int | None, container: str) -> str:
    """화질·컨테이너에 맞춘 yt-dlp 포맷 셀렉터.
    MP4 선택 시 H.264+AAC만 골라야 재인코딩 없이 무손실로 담긴다."""
    h = f"[height<={height}]" if height else ""
    if container == "mp4":
        return (
            f"bestvideo{h}[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            f"bestvideo{h}[ext=mp4]+bestaudio[ext=m4a]/"
            f"best{h}[ext=mp4]/best{h}"
        )
    return f"bestvideo{h}+bestaudio/best{h}"


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
    """webbrowser.open() 으로 열기 전에 스킴 검증.
    외부 JSON 에서 받은 URL이 javascript:/file: 등으로 변조되는 걸 차단."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def safe_open_url(url: str | None) -> bool:
    if is_safe_http_url(url):
        webbrowser.open(url)  # type: ignore[arg-type]
        return True
    log.warning("opening unsafe url blocked: %r", url)
    return False


def ffmpeg_cache_dir() -> Path:
    """자동 설치된 ffmpeg.exe 가 저장되는 위치."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_ID / "ffmpeg" / "bin"


def ytdlp_cache_dir() -> str:
    """yt-dlp 플레이어 JS/서명 캐시 위치. 분석(extract_info)이 매번 플레이어 JS 를
    다시 받지 않도록 영속 캐시를 둬 반복 분석을 조금 더 빠르게 한다."""
    return str(Path(os.environ.get("LOCALAPPDATA") or Path.home())
               / APP_ID / "ytdlp-cache")


def config_path() -> Path:
    """사용자 설정(config.json) 저장 위치. 로그/ffmpeg 캐시와 같은 앱 폴더."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_ID / "config.json"


def find_ffmpeg() -> str | None:
    """ffmpeg 경로 탐색. 자동 설치 캐시 → PyInstaller 번들 → PATH → 일반 위치 순."""
    from shutil import which

    candidates: list[Path] = [ffmpeg_cache_dir()]
    if getattr(sys, "frozen", False):
        candidates.append(Path(getattr(sys, "_MEIPASS", "")))
        candidates.append(Path(sys.executable).parent)

    for c in candidates:
        if (c / "ffmpeg.exe").exists():
            return str(c)

    p = which("ffmpeg")
    if p:
        return str(Path(p).parent)

    for guess in [
        Path(os.environ.get("LOCALAPPDATA", "")) / "ffmpeg",
        Path("C:/ffmpeg/bin"),
        Path("C:/Program Files/ffmpeg/bin"),
    ]:
        if guess.exists():
            for sub in guess.rglob("ffmpeg.exe"):
                return str(sub.parent)
    return None


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
    """쿠키 DB 가 실제로 존재하는 브라우저를 우선순위대로 찾아 yt-dlp 이름을
    돌려준다(없으면 None). 로그인 필요한 영상에서만 자동 폴백용으로 쓴다."""
    local = Path(os.environ.get("LOCALAPPDATA", "") or "")
    appdata = Path(os.environ.get("APPDATA", "") or "")
    candidates = [
        ("chrome",  local / "Google" / "Chrome" / "User Data"),
        ("edge",    local / "Microsoft" / "Edge" / "User Data"),
        ("whale",   local / "Naver" / "Naver Whale" / "User Data"),
        ("brave",   local / "BraveSoftware" / "Brave-Browser" / "User Data"),
        ("firefox", appdata / "Mozilla" / "Firefox" / "Profiles"),
        ("vivaldi", local / "Vivaldi" / "User Data"),
        ("opera",   appdata / "Opera Software" / "Opera Stable"),
    ]
    for name, path in candidates:
        try:
            if path.exists():
                return name
        except OSError:
            pass
    return None


def looks_like_drm(err: str) -> bool:
    """오류 메시지가 DRM 보호 콘텐츠로 보이면 True. 이때는 재시도/우회하지 않고
    'DRM 미지원'을 사용자에게 안내한 뒤 중단한다(DRM 우회는 불법이라 미구현)."""
    e = (err or "").lower()
    return any(k in e for k in DRM_HINTS)


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
        opts: dict = {"quiet": True, "no_warnings": True, "skip_download": True,
                      "cachedir": ytdlp_cache_dir()}
        if cookies_browser:
            # (browser, profile, keyring, container) 4-튜플이어야 한다.
            opts["cookiesfrombrowser"] = (cookies_browser, None, None, None)
        with yt_dlp.YoutubeDL(opts) as ydl:
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
        formats = info.get("formats") or []
        hs = {f.get("height") for f in formats if isinstance(f.get("height"), int)}
        return sorted(hs)

    @staticmethod
    def _thumb(info: dict) -> str:
        """가장 적당한 썸네일 URL 하나를 고른다 (없으면 빈 문자열)."""
        t = info.get("thumbnail")
        if t:
            return str(t)
        for th in reversed(info.get("thumbnails") or []):
            u = th.get("url")
            if u:
                return str(u)
        return ""


# ============================================================
#  서비스: 다운로드
# ============================================================

# 사용자가 취소했을 때 on_done(err) 로 전달되는 내부 신호값 (실제 에러 아님).
DOWNLOAD_CANCELLED = "__download_cancelled__"


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


class Downloader:
    """yt-dlp 다운로드 래퍼. progress/완료 콜백 호출."""

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
        # 취소 신호. run() 시작 시 clear, cancel() 시 set.
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """진행 중인 다운로드를 다음 진행률 콜백 시점에 중단시킨다."""
        self._cancel_event.set()

    def _progress_hook(self, d: dict) -> None:
        """yt-dlp progress hook 래퍼. 취소 요청이 있으면 DownloadCancelled 을
        던져 다운로드를 즉시 멈추고, 아니면 앱 콜백으로 위임한다."""
        if self._cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled()
        self._on_progress(d)

    def _postprocessor_hook(self, d: dict) -> None:
        """yt-dlp postprocessor hook. 후처리(Merger/추출 등)가 끝나면 해당
        항목을 개별 완료 처리한다."""
        if d.get("status") != "finished" or self._on_item_done is None:
            return
        info = d.get("info_dict") or {}
        vid = str(info.get("id") or "")
        if vid:
            # 후처리 결과의 최종 경로. 진행률 훅이 잡은 임시 조각 파일명 대신
            # 이걸 써야 '재생/폴더 열기'가 올바른 최종 파일을 가리킨다.
            filepath = info.get("filepath") or None
            self._on_item_done(vid, filepath)

    def run(self, cfg: DownloadConfig) -> None:
        """단계적 폴백으로 다운로드 성공률을 높인다.

        1단계(yt-dlp): 기본 → 브라우저 쿠키 → 브라우저 헤더 → 포맷 낮춤 순으로
                       하나라도 성공할 때까지 재시도.
        2단계(yt-dlp 미지원): 페이지 HTML 에서 HLS/DASH 매니페스트를 직접 찾아
                       yt-dlp 에 재투입. (옵트인 시) Playwright 로 네트워크 가로채기.
        DRM 감지 시: 우회하지 않고 즉시 안내 후 중단."""
        self._cancel_event.clear()
        base = self._base_opts(cfg)
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
                with yt_dlp.YoutubeDL({**base, **extra}) as ydl:
                    ydl.download([cfg.url])
                self._on_done(True, None)
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
                last_err = msg
                log.info("attempt failed (%s): %s", label,
                         (msg.splitlines()[-1] if msg else "")[:160])

        # ── 2단계: yt-dlp 가 전부 실패 → 직접 스트림 탐색 ──────
        if not self._cancel_event.is_set():
            try:
                if self._try_stage2(cfg, base):
                    self._on_done(True, None)
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
            self._on_done(False, last_err or "다운로드에 실패했습니다.")

    def _base_opts(self, cfg: DownloadConfig) -> dict:
        """모든 시도가 공유하는 기본 yt-dlp 옵션 (포맷/쿠키/헤더는 시도별로 덮음)."""
        opts: dict = {
            "outtmpl": os.path.join(cfg.out_dir, "%(title)s [%(id)s].%(ext)s"),
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
        # 분석 단계에서 쿠키가 필요했으면 그걸, 아니면 설치된 브라우저를 탐지.
        cookie = cfg.cookies_browser or detect_cookie_browser()
        cookie_opt = (
            {"cookiesfrombrowser": (cookie, None, None, None)} if cookie else {}
        )

        plan: list[tuple[str, dict]] = []
        # 1) 기본 (분석 때 쿠키가 필요했던 경우엔 그 쿠키 포함)
        first: dict = {"format": fmt}
        if cfg.cookies_browser:
            first["cookiesfrombrowser"] = (cfg.cookies_browser, None, None, None)
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
            plan.append(("최저 화질로 재시도", {"format": "worst", "http_headers": headers}))
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
        """페이지 HTML 에서 .m3u8/.mpd 링크를 직접 찾는다 (HLS 우선)."""
        req = urllib.request.Request(page_url, headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml,*/*",
        })
        try:
            with urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT) as resp:
                raw = resp.read(5 * 1024 * 1024)        # 5MB 상한 (메모리 보호)
            html = raw.decode("utf-8", "replace").replace("\\/", "/")
        except (urllib.error.URLError, OSError, ValueError):
            return None

        m3u8: list[str] = []
        mpd: list[str] = []
        seen: set[str] = set()
        for u in MANIFEST_RE.findall(html):
            u = u.replace("&amp;", "&")
            if u in seen:
                continue
            seen.add(u)
            (m3u8 if ".m3u8" in u.lower() else mpd).append(u)
        picks = m3u8 or mpd
        return picks[0] if picks else None

    def _download_manifest(self, manifest_url: str,
                           cfg: DownloadConfig, base: dict) -> bool:
        """찾은 매니페스트(HLS/DASH)를 yt-dlp 로 받는다. 취소는 상위로 전파."""
        opts = {**base, "format": "best", "http_headers": self._headers_for(cfg.url)}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([manifest_url])
            return True
        except yt_dlp.utils.DownloadCancelled:
            raise
        except Exception as e:
            msg = str(e)
            log.info("manifest download failed: %s",
                     (msg.splitlines()[-1] if msg else "")[:160])
            return False

    @staticmethod
    def _playwright_sniff(page_url: str) -> str | None:
        """(옵트인) 헤드리스 브라우저로 페이지를 열어 네트워크 요청에서 스트림
        URL(.m3u8/.mpd)을 가로챈다. Playwright 가 설치돼 있을 때만 동작한다.
        설치 안 돼 있으면 조용히 건너뛴다(일반 사용자는 설치 불필요)."""
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
                        found.append(u)

                page.on("response", _on_response)
                try:
                    page.goto(page_url, wait_until="networkidle", timeout=30000)
                except Exception:
                    pass   # 타임아웃이어도 그때까지 잡은 URL 은 활용
                browser.close()
        except Exception as e:
            log.info("playwright sniff failed: %s", e)
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
    """version.json 해석 결과를 UI에 안전한 형태로 전달."""
    kind: str           # "killswitch" | "force_update" | "new_version" | "uptodate"
    message: str = ""
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

        return self._interpret(data)

    def _interpret(self, data: dict) -> UpdateDecision:
        url = data.get("download_url")
        safe_url = url if is_safe_http_url(url) else None
        current = self.current_version

        if data.get("killswitch"):
            return UpdateDecision(
                kind="killswitch",
                message=str(data.get("killswitch_message")
                            or "현재 사용이 일시 중단되었습니다."),
                download_url=safe_url,
            )

        min_ver = str(data.get("min_supported_version", "0"))
        if compare_versions(current, min_ver) < 0:
            return UpdateDecision(
                kind="force_update",
                message=f"현재 버전(v{self.current_version})은 더 이상 지원되지 않습니다.",
                download_url=safe_url,
            )

        latest_str = str(data.get("latest_version", self.current_version))
        if compare_versions(current, latest_str) < 0:
            return UpdateDecision(
                kind="new_version",
                latest_version=latest_str,
                release_notes=str(data.get("release_notes", "")),
                download_url=safe_url,
            )

        return UpdateDecision(kind="uptodate")


# ============================================================
#  서비스: ffmpeg 자동 설치
# ============================================================

@dataclass
class FfmpegProgress:
    stage: str        # "downloading" | "extracting" | "done" | "error"
    pct: float = 0.0
    message: str = ""


class FfmpegInstaller:
    """BtbN/FFmpeg-Builds 빌드를 받아 LOCALAPPDATA 에 캐시.

    무결성 보호 (롤링 'latest' 태그와 호환되는 3중 방어):
    (a) 다운로드한 zip 의 크기 sanity 범위 확인 (오류 HTML/잘린 파일 차단)
    (b) GitHub API 가 게시하는 에셋의 authoritative SHA256(digest)을 받아
        다운로드 바이트와 대조 → 전송 중 손상/변조 탐지 (다운로드와 분리된
        api.github.com TLS 채널). API 를 못 받으면 경고만 남기고 (a)(c) 로만
        진행해 설치를 막지 않는다.
    (c) zip 구조 유효성 + bin/ffmpeg.exe 멤버 존재 확인

    EXPECTED_SHA256 에 해시를 직접 박으면 API 조회 대신 그 값으로 고정 핀 한다
    (가장 강하지만 'latest' 가 갱신되면 직접 갱신 필요). 비우면 (b) 동적 조회.
    """

    GITHUB_REPO = "BtbN/FFmpeg-Builds"
    RELEASE_TAG = "latest"
    ASSET_NAME = "ffmpeg-master-latest-win64-gpl.zip"

    URL = (
        f"https://github.com/{GITHUB_REPO}/releases/download/"
        f"{RELEASE_TAG}/{ASSET_NAME}"
    )
    # 에셋 메타데이터(특히 sha256 digest) 조회용 GitHub API.
    API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}"

    # 해시를 직접 고정하려면 소문자 hex 를 넣는다. 비우면 API 동적 조회.
    EXPECTED_SHA256 = ""
    API_TIMEOUT = 10               # 초 (digest 조회)
    API_MAX_BYTES = 1_000_000      # 릴리스 JSON 응답 크기 상한 (메모리 보호)

    DOWNLOAD_TIMEOUT = 60  # 초
    # 무결성: 받은 zip 크기 sanity 범위 (정상 win64-gpl zip 은 수십~수백 MB)
    MIN_ZIP_BYTES = 5 * 1024 * 1024          # 5MB 미만이면 손상/오류 페이지로 간주
    MAX_ZIP_BYTES = 500 * 1024 * 1024        # 500MB 초과면 비정상으로 간주

    def install(self, on_progress: Callable[[FfmpegProgress], None]) -> bool:
        target_dir = ffmpeg_cache_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "ffmpeg.exe"
        if target.exists():
            on_progress(FfmpegProgress(stage="done", pct=100.0, message="이미 설치됨"))
            return True

        tmp_zip = target_dir.parent / "ffmpeg-tmp.zip"
        try:
            self._download(self.URL, tmp_zip, on_progress)
        except Exception as e:
            log.exception("ffmpeg download failed")
            on_progress(FfmpegProgress(stage="error", message=f"다운로드 실패: {e}"))
            self._cleanup(tmp_zip)
            return False

        # 무결성 검증 (크기/해시/zip 구조). 실패 시 추출하지 않음.
        on_progress(FfmpegProgress(stage="extracting", message="무결성 검증 중..."))
        expected = self.EXPECTED_SHA256 or self._fetch_expected_digest()
        try:
            self._verify_zip(tmp_zip, expected)
        except Exception as e:
            log.error("ffmpeg integrity check failed: %s", e)
            on_progress(FfmpegProgress(stage="error", message=f"무결성 검증 실패: {e}"))
            self._cleanup(tmp_zip)
            return False

        on_progress(FfmpegProgress(stage="extracting", message="압축 해제 중..."))
        try:
            self._extract(tmp_zip, target)
        except Exception as e:
            log.exception("ffmpeg extract failed")
            on_progress(FfmpegProgress(stage="error", message=f"압축 해제 실패: {e}"))
            return False
        finally:
            self._cleanup(tmp_zip)

        on_progress(FfmpegProgress(stage="done", pct=100.0, message="설치 완료"))
        return True

    def _download(self, url: str, dest: Path,
                  on_progress: Callable[[FfmpegProgress], None]) -> None:
        req = urllib.request.Request(
            url, headers={"User-Agent": HTTP_UA})
        with urllib.request.urlopen(req, timeout=self.DOWNLOAD_TIMEOUT) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    pct = (done / total * 100) if total else 0
                    on_progress(FfmpegProgress(
                        stage="downloading", pct=pct,
                        message=f"{done/1024/1024:.1f} / {total/1024/1024:.1f} MB",
                    ))

    @classmethod
    def _fetch_expected_digest(cls) -> str | None:
        """GitHub API 에서 에셋의 sha256 digest 를 가져온다 (소문자 hex).
        네트워크/파싱 실패 시 None → 해시 검증은 생략하되 설치는 계속한다."""
        try:
            req = urllib.request.Request(
                cls.API_URL,
                headers={"User-Agent": HTTP_UA,
                         "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req, timeout=cls.API_TIMEOUT) as resp:
                raw = resp.read(cls.API_MAX_BYTES)
            data = json.loads(raw.decode("utf-8"))
            for asset in data.get("assets") or []:
                if asset.get("name") == cls.ASSET_NAME:
                    digest = str(asset.get("digest") or "")
                    if digest.startswith("sha256:"):
                        return digest.split(":", 1)[1].strip().lower()
                    return None
        except Exception:
            log.info("ffmpeg digest fetch skipped (offline or API error)")
        return None

    @classmethod
    def _verify_zip(cls, zip_path: Path, expected_sha256: str | None = None) -> None:
        """다운로드한 zip 의 무결성을 검증한다.
        - 파일 크기 sanity 범위 확인 (오류 HTML/잘린 파일 차단)
        - expected_sha256 이 있으면 SHA256 대조 (불일치 시 예외)
        - zip 구조 유효성 + bin/ffmpeg.exe 멤버 존재 확인
        실패하면 예외를 던진다."""
        size = zip_path.stat().st_size
        if not (cls.MIN_ZIP_BYTES <= size <= cls.MAX_ZIP_BYTES):
            raise RuntimeError(
                f"받은 파일 크기가 비정상입니다 ({size/1024/1024:.1f} MB)"
            )

        # (b) authoritative 해시가 있으면 바이트 대조
        if expected_sha256:
            digest = cls._sha256(zip_path)
            if digest.lower() != expected_sha256.strip().lower():
                raise RuntimeError("SHA256 해시가 일치하지 않습니다 (변조 의심)")

        # (b) zip 유효성 + 예상 구조(bin/ffmpeg.exe) 확인
        with zipfile.ZipFile(zip_path) as z:
            if z.testzip() is not None:
                raise RuntimeError("zip 내부에 손상된 항목이 있습니다")
            has_ffmpeg = any(
                n.replace("\\", "/").endswith("/bin/ffmpeg.exe")
                for n in z.namelist()
            )
            if not has_ffmpeg:
                raise RuntimeError("zip 구조가 예상과 다릅니다 (ffmpeg.exe 없음)")

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _extract(zip_path: Path, target: Path) -> None:
        with zipfile.ZipFile(zip_path) as z:
            member = next(
                (n for n in z.namelist()
                 if n.replace("\\", "/").endswith("/bin/ffmpeg.exe")),
                None,
            )
            if not member:
                raise RuntimeError("zip 안에서 ffmpeg.exe 를 찾지 못했습니다")
            with z.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

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
    """사용자 설정을 LOCALAPPDATA/config.json 에 읽고 쓴다. UI 비의존.

    손상/부재 시 빈 dict 로 안전하게 폴백하고, 저장은 임시파일→교체로 원자적
    처리해 쓰기 중 중단되어도 기존 설정이 깨지지 않는다."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_path()

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)   # 원자적 교체
        except OSError:
            log.exception("settings save failed")


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
        self._ffmpeg_dir = find_ffmpeg()
        self._auto_cookie_browser: str | None = None
        self._files: dict[str, str] = {}              # job_id → 최종 파일 경로
        self._active: dict[str, Downloader] = {}      # 동시 진행 중 (job_id → Downloader)
        self._maxed = False

    def set_window(self, w) -> None:
        self._window = w

    # ---------------- 내부 헬퍼 ----------------

    def _js(self, code: str) -> None:
        w = self._window
        if w is None:
            return
        try:
            w.evaluate_js(code)
        except Exception:
            pass

    @staticmethod
    def _default_dir() -> str:
        return str(Path.home() / "Downloads" / "싹싹김치 다운로드")

    def _save_short(self) -> str:
        parts = [p for p in re.split(r"[\\/]", self._cfg.get("save_dir", self._default_dir())) if p]
        full = self._cfg.get("save_dir", self._default_dir())
        return "…\\" + "\\".join(parts[-2:]) if len(parts) > 2 else full

    def _qualities_for(self, result: AnalysisResult) -> list[str]:
        if not result.heights:
            return [lbl for lbl, _ in QUALITY_TABLE]
        max_h = max(result.heights)
        return [lbl for lbl, h in QUALITY_TABLE if h is None or h == -1 or h <= max_h]

    def _probe_auto(self, url: str) -> tuple[AnalysisResult, str | None]:
        try:
            return self._analyzer.probe(url, None), None
        except Exception as e:
            if not looks_like_login_error(str(e)):
                raise
            browser = detect_cookie_browser()
            if not browser:
                raise
            log.info("retrying analysis with %s cookies", browser)
            return self._analyzer.probe(url, browser), browser

    # ---------------- JS 호출용 API ----------------

    def get_init(self) -> dict:
        log.info("bridge ready: get_init called")
        return {
            "app_title": APP_TITLE,
            "version": APP_VERSION,
            "ffmpeg_ok": self._ffmpeg_dir is not None,
            "tos_accepted": self._cfg.get("tos_accepted") == TOS_VERSION,
            "save_dir": self._cfg.get("save_dir", self._default_dir()),
            "quality": self._cfg.get("quality", "1080p (FHD)"),
            "format": self._cfg.get("format", FORMAT_TABLE[0][0]),
            "qualities": [lbl for lbl, _ in QUALITY_TABLE],
            "formats": [lbl for lbl, _ in FORMAT_TABLE],
            "promo_url": PROMO_URL,
            "hint": "링크를 붙여넣으면 영상 정보 팝업이 뜹니다. 화질·포맷을 고르고 다운로드하세요.",
        }

    def accept_tos(self) -> None:
        self._cfg["tos_accepted"] = TOS_VERSION
        self._settings.save(self._cfg)

    def read_clipboard(self) -> str:
        return read_clipboard_text()

    def analyze(self, url: str) -> dict:
        url = (url or "").strip()
        if not URL_RE.match(url):
            return {"ok": False, "error": "올바른 링크(http/https)가 아닙니다"}
        try:
            result, browser = self._probe_auto(url)
            self._auto_cookie_browser = browser
        except Exception as e:
            log.info("analyze failed: %s", e)
            return {"ok": False, "error": (str(e).splitlines()[-1] if str(e) else "분석 실패")[:140]}
        qs = self._qualities_for(result)
        default_q = self._cfg.get("quality", "1080p (FHD)")
        if default_q not in qs:
            default_q = "1080p (FHD)" if "1080p (FHD)" in qs else qs[0]
        return {
            "ok": True, "url": url, "title": result.title, "meta": result.meta,
            "thumbnail": result.thumbnail, "qualities": qs, "quality": default_q,
            "save_short": self._save_short(),
        }

    def start_download(self, job_id: str, url: str, quality: str, fmt: str) -> dict:
        url = (url or "").strip()
        if not URL_RE.match(url):
            return {"ok": False, "error": "올바른 링크(http/https)가 아닙니다"}
        is_audio = "음성만" in quality
        if self._ffmpeg_dir is None and not is_audio:
            return {"ok": False, "error": "ffmpeg 준비 중입니다. 잠시 후 다시 시도하세요"}
        save_dir = Path(self._cfg.get("save_dir", self._default_dir()))
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"저장 폴더를 만들 수 없습니다: {e}"}
        # 마지막 선택을 기본값으로 기억(다음 분석 팝업의 기본 화질/포맷)
        self._cfg["quality"] = quality
        self._cfg["format"] = fmt
        self._settings.save(self._cfg)

        height = dict(QUALITY_TABLE).get(quality)
        container = dict(FORMAT_TABLE).get(fmt, "mp4")
        fmt_str = "bestaudio/best" if is_audio else build_format_selector(height, container)
        cfg = DownloadConfig(
            url=url, out_dir=str(save_dir), format_str=fmt_str,
            is_audio_only=is_audio, container=container, ffmpeg_dir=self._ffmpeg_dir,
            cookies_browser=self._auto_cookie_browser, embed_metadata=True,
            use_playwright=ENABLE_PLAYWRIGHT,   # 소스(개인)=True, 배포 exe=False
        )
        # 동시 다운로드: 작업마다 독립 Downloader 를 만들고 콜백에 job_id 를 바인딩한다.
        dl = Downloader(
            on_progress=lambda d, j=job_id: self._on_progress(j, d),
            on_done=lambda ok, e, j=job_id: self._on_done(j, ok, e),
            on_item_done=lambda vid, fp=None, j=job_id: self._on_item_done(j, fp),
            on_status=lambda t, j=job_id: self._on_status(j, t),
        )
        self._active[job_id] = dl
        self._js("setBusy(true)")
        log.info("download start id=%s url=%s (active=%d)", job_id, url, len(self._active))
        threading.Thread(target=dl.run, args=(cfg,), daemon=True).start()
        return {"ok": True}

    def cancel_download(self) -> None:
        # 진행 중인 모든 다운로드를 취소한다(전체 취소 버튼).
        for dl in list(self._active.values()):
            dl.cancel()

    def forget_item(self, job_id: str) -> None:
        """목록에서 항목 제거(X): 진행 중이면 취소(끝나면 _on_done 이 정리)."""
        dl = self._active.get(job_id)
        if dl:
            dl.cancel()
        self._files.pop(job_id, None)
        if not self._active:
            self._js("setBusy(false)")

    def pick_folder(self) -> str | None:
        try:
            res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            return None
        if not res:
            return None
        d = res[0] if isinstance(res, (list, tuple)) else res
        self._cfg["save_dir"] = d
        self._settings.save(self._cfg)
        return d

    def open_folder(self) -> None:
        d = self._cfg.get("save_dir", self._default_dir())
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            os.startfile(d)  # noqa: S606
        except OSError:
            log.exception("open folder failed")

    def open_item(self, job_id: str) -> None:
        fp = self._files.get(job_id)
        if not fp:
            self.open_folder()                       # 경로 모르면 저장 폴더라도 연다
            return
        p = Path(fp)
        # 파일을 선택한 채 탐색기 열기. explorer 는 반드시 /select,"전체경로" 형식의
        # '문자열'로 넘겨야 한다. 리스트로 넘기면 따옴표가 통째로 감싸져 explorer 가
        # 경로를 못 알아듣고 기본(문서) 폴더를 열어버린다 — 그게 이 버그였다.
        if p.exists():
            try:
                subprocess.run(f'explorer /select,"{os.path.normpath(str(p))}"')
                return
            except Exception:
                pass
        # 폴백: 파일이 있던 폴더(없으면 저장 폴더)를 연다
        folder = p.parent if p.parent.exists() else Path(
            self._cfg.get("save_dir", self._default_dir()))
        try:
            os.startfile(str(folder))  # noqa: S606
        except OSError:
            self.open_folder()

    def play_item(self, job_id: str) -> None:
        fp = self._files.get(job_id)
        if fp and Path(fp).exists():
            try:
                os.startfile(fp)  # noqa: S606
            except OSError:
                pass

    def open_url(self, url: str) -> None:
        safe_open_url(url)

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
        # 종료 시 광고 1회 노출 → JS 가 exit_confirmed 로 실제 종료
        self._js("showExit()")

    def exit_confirmed(self) -> None:
        try:
            self._window.destroy()
        except Exception:
            pass

    # ---------------- 다운로더 콜백 (다운로드 스레드 → JS) ----------------

    def _on_progress(self, jid: str, d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            pct = (done / total * 100) if total else 0
            self._js(
                f"dlProgress('{jid}',{{pct:{pct:.1f},"
                f"done:{done / 1048576:.2f},total:{total / 1048576:.2f},"
                f"speed:{(speed or 0) / 1048576:.2f}}})"
            )
        elif status == "finished":
            self._js(f"dlMerging('{jid}')")

    def _on_status(self, jid: str, text: str) -> None:
        """폴백 재시도/2단계 진입 시 해당 항목에 진행 상태 문구를 보여준다."""
        self._js(f"dlStatus('{jid}',{json.dumps(text)})")

    def _on_item_done(self, jid: str, filepath: str | None = None) -> None:
        if filepath:
            self._files[jid] = filepath
        fp = self._files.get(jid, "")
        size = ""
        try:
            if fp:
                size = f"{Path(fp).stat().st_size / 1048576:.1f} MB"
        except OSError:
            pass
        fname = Path(fp).name if fp else ""
        self._js(f"dlDone('{jid}',{json.dumps({'size': size, 'file': fname})})")

    def _on_done(self, jid: str, ok: bool, err: str | None) -> None:
        self._active.pop(jid, None)
        if not ok and err == DOWNLOAD_CANCELLED:
            self._js(f"dlCancelled('{jid}')")
        elif not ok:
            msg = (err or "실패").splitlines()[-1][:120]
            self._js(f"dlError('{jid}',{json.dumps(msg)})")
        if not self._active:
            self._js("setBusy(false)")

    # ---------------- 백그라운드 (시작 후) ----------------

    def post_init(self) -> None:
        threading.Thread(target=self._kill_and_update, daemon=True).start()
        if self._ffmpeg_dir is None:
            threading.Thread(target=self._install_ffmpeg, daemon=True).start()
        # 클립보드 자동 감지는 두지 않는다 — 사용자가 링크 칸에 '붙여넣을 때만'
        # 팝업이 뜨게 한다(복사만 해도 자동으로 뜨는 동작 제거).

    def _kill_and_update(self) -> None:
        decision = self._updater.fetch_decision()
        if decision is None:
            # 오프라인: 이전에 받은 정지가 있으면 유지
            if self._cfg.get("killed"):
                self._kill(self._cfg.get("killed_message") or "현재 사용이 중단되었습니다.")
            return
        if decision.kind == "killswitch":
            self._cfg["killed"] = True
            self._cfg["killed_message"] = decision.message
            self._settings.save(self._cfg)
            self._kill(decision.message)
        elif decision.kind == "force_update":
            self._js(f"alert({json.dumps(decision.message + ' 업데이트가 필요합니다.')})")
            safe_open_url(decision.download_url)
        else:
            if self._cfg.get("killed"):
                self._cfg["killed"] = False
                self._cfg["killed_message"] = ""
                self._settings.save(self._cfg)

    def _kill(self, msg: str) -> None:
        self._js(f"alert({json.dumps(msg)})")
        self.exit_confirmed()

    def _install_ffmpeg(self) -> None:
        def cb(p: FfmpegProgress) -> None:
            if p.stage == "downloading":
                self._js(f"ffmpegStatus({json.dumps('필요한 파일 받는 중… ' + p.message)},false)")
            elif p.stage == "extracting":
                self._js(f"ffmpegStatus({json.dumps('설치 중…')},false)")
            elif p.stage == "done":
                self._ffmpeg_dir = find_ffmpeg()
                self._js("ffmpegStatus('준비 완료',true)")
            elif p.stage == "error":
                self._js(f"ffmpegStatus({json.dumps('파일 설치 실패: ' + p.message)},false)")
        self._installer.install(cb)


# ============================================================
#  엔트리포인트
# ============================================================

def main() -> None:
    log_file = setup_logging()
    log.info("starting %s v%s (pywebview), log=%s", APP_TITLE, APP_VERSION, log_file)
    api = Api()
    window = webview.create_window(
        f"{APP_TITLE}",
        _ui_html_path(),
        js_api=api,
        width=760, height=624, min_size=(680, 560),
        frameless=True, easy_drag=False,
        background_color="#f4f5f6",
    )
    api.set_window(window)
    try:
        webview.start(api.post_init, gui="edgechromium")
    except Exception:
        log.exception("webview start failed")
        raise


if __name__ == "__main__":
    main()
