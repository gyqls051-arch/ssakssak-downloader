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
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from typing import Callable

import yt_dlp

# Pillow 는 썸네일 표시에만 사용. 없어도 앱은 동작 (회색 박스로 표시).
try:
    from PIL import Image, ImageTk  # type: ignore
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# ============================================================
#  메타데이터 / 설정
# ============================================================

APP_TITLE = "싹싹김치 다운로드"
APP_VERSION = "1.2"
# APP_ID 는 로그/ffmpeg 캐시/config.json 폴더 경로(%LOCALAPPDATA%\<APP_ID>)에
# 쓰인다. 브랜드 식별자라 한 번 정하면 유지한다(바꾸면 기존 사용자 데이터 고아).
APP_ID = "SsakSsakKimchiDownloader"

# 무료 일일 다운로드 한도 (완료된 영상 개수 기준, 자정에 리셋).
# config.json 의 카운트라 삭제로 우회 가능 — 엄격한 DRM 이 아니라 과도한
# 사용 억제용. 무제한 전환은 _is_unlimited() 한 곳만 교체.
DAILY_DOWNLOAD_LIMIT = 20

# 첫 실행 시 받을 이용약관/면책 동의 버전. 약관을 바꾸면 올려 재동의 받는다.
TOS_VERSION = "1"

# 제휴 광고 배너 — 외부 광고주처럼 노출된다 (앱 소유와 무관한 일반 광고 슬롯).
# ※ 아래 도메인을 실제 홍보 대상 홈페이지 주소로 교체하세요.
#    UTM 파라미터로 소유관계 노출 없이 유입량만 측정할 수 있다.
PROMO_URL = "https://offcut.kr/?utm_source=ssakssak_downloader&utm_medium=app_banner"
PROMO_TEXT = "광고 ｜ OFFCUT STUDIO — 영상 제작 올인원 워크스페이스 · 자세히 보기 →"

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
VERSION_INFO_URL = "https://raw.githubusercontent.com/<your-account>/ssakssak-downloader/main/version.json"
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

# 로그인 쿠키를 읽어올 브라우저 라벨 → yt-dlp 브라우저 이름(None=사용 안 함).
# 인스타그램 비공개/로그인 영상, 멤버십 영상 등을 받을 때 사용한다.
# Firefox 를 권장: Chrome/Edge 는 2024-07 이후 App-Bound Encryption 으로
# 외부 쿠키 추출이 막히는 경우가 많다. (해당 브라우저는 닫은 상태여야 안정적)
COOKIE_BROWSERS: list[tuple[str, str | None]] = [
    ("사용 안 함",        None),
    ("Firefox (권장)",   "firefox"),
    ("Chrome",          "chrome"),
    ("Edge",            "edge"),
    ("Brave",           "brave"),
    ("Opera",           "opera"),
    ("Vivaldi",         "vivaldi"),
    ("Whale",           "whale"),
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


def preferred_font_family() -> str:
    """Win11의 'Segoe UI Variable' 우선, 없으면 'Segoe UI'."""
    try:
        if "Segoe UI Variable" in tkfont.families():
            return "Segoe UI Variable"
    except tk.TclError:
        pass
    return "Segoe UI"


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
        opts: dict = {"quiet": True, "no_warnings": True, "skip_download": True}
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
    embed_subs: bool = False             # 자막 다운로드 후 삽입 (영상 전용)
    sponsorblock: bool = False           # SponsorBlock 광고/홍보 구간 제거 (영상 전용)


class Downloader:
    """yt-dlp 다운로드 래퍼. progress/완료 콜백 호출."""

    def __init__(self,
                 on_progress: Callable[[dict], None],
                 on_done: Callable[[bool, str | None], None],
                 on_item_done: Callable[[str, str | None], None] | None = None):
        self._on_progress = on_progress
        self._on_done = on_done
        # 항목(영상) 1개의 후처리까지 끝났을 때 호출. 플레이리스트에서
        # 각 항목을 개별 완료 처리하기 위한 콜백 (vid, 최종 경로 전달).
        self._on_item_done = on_item_done
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
        self._cancel_event.clear()
        opts: dict = {
            "outtmpl": os.path.join(cfg.out_dir, "%(title)s [%(id)s].%(ext)s"),
            "format": cfg.format_str,
            "noplaylist": False,
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._postprocessor_hook],
            "quiet": True,
            "no_warnings": True,
            "concurrent_fragment_downloads": 4,
            "retries": 10,
            "fragment_retries": 10,
        }
        if cfg.ffmpeg_dir:
            opts["ffmpeg_location"] = cfg.ffmpeg_dir
        if cfg.cookies_browser:
            # (browser, profile, keyring, container) 4-튜플이어야 한다.
            opts["cookiesfrombrowser"] = (cfg.cookies_browser, None, None, None)
        self._configure_output(cfg, opts)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([cfg.url])
            self._on_done(True, None)
        except yt_dlp.utils.DownloadCancelled:
            log.info("download cancelled by user")
            self._on_done(False, DOWNLOAD_CANCELLED)
        except Exception as e:
            log.exception("download failed")
            self._on_done(False, str(e))

    @staticmethod
    def _configure_output(cfg: DownloadConfig, opts: dict) -> None:
        """출력 포맷 + 후처리 파이프라인을 opts 에 채운다.

        후처리 추가 순서는 yt-dlp CLI(parse_options)가 생성하는 순서와 동일하게
        맞춘다: [SponsorBlock] → [EmbedSubtitle] → [ModifyChapters] →
        FFmpegMetadata → EmbedThumbnail. 순서가 어긋나면 챕터/자막/메타데이터가
        서로를 덮어쓸 수 있어 그대로 따른다."""
        pps: list[dict] = []
        if cfg.is_audio_only:
            pps.append({"key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3", "preferredquality": "192"})
        else:
            opts["merge_output_format"] = cfg.container
            sb_cats = {"sponsor", "selfpromo", "interaction"}
            if cfg.sponsorblock:
                pps.append({"key": "SponsorBlock", "categories": sb_cats,
                            "when": "after_filter"})
            if cfg.embed_subs:
                opts["writesubtitles"] = True
                opts["subtitleslangs"] = ["ko", "en"]
                pps.append({"key": "FFmpegEmbedSubtitle",
                            "already_have_subtitle": False})
            if cfg.sponsorblock:
                pps.append({"key": "ModifyChapters",
                            "remove_sponsor_segments": sb_cats})

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
                headers={"User-Agent": f"{APP_TITLE}/{self.current_version}"},
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
            url, headers={"User-Agent": f"{APP_TITLE}/{APP_VERSION}"})
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
                headers={"User-Agent": f"{APP_TITLE}/{APP_VERSION}",
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
#  UI
# ============================================================

PAD_OUTER = 28
PAD_CARD = 22


def apply_theme(root: tk.Tk) -> None:
    """sv-ttk(Win11 라이트) 가능하면 적용. 없으면 clam 폴백."""
    try:
        import sv_ttk  # noqa: WPS433
        sv_ttk.set_theme("light")
    except Exception:
        log.info("sv_ttk not available, using fallback")
        try:
            ttk.Style(root).theme_use("clam")
        except tk.TclError:
            pass

    # 폰트 일관성: 기본 ttk 폰트들을 Segoe UI Variable로
    fam = preferred_font_family()
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            f = tkfont.nametofont(name)
            f.configure(family=fam, size=10)
        except tk.TclError:
            pass

    style = ttk.Style(root)
    # 카드용 보조 스타일 — sv_ttk 위에 살짝만 얹는다.
    style.configure("Title.TLabel", font=(fam, 20, "bold"))
    style.configure("Subtitle.TLabel", foreground=COLOR_MUTED, font=(fam, 9))
    style.configure("CardTitle.TLabel", font=(fam, 13, "bold"))
    style.configure("CardMuted.TLabel", foreground=COLOR_MUTED, font=(fam, 9))
    style.configure("Hero.TEntry", padding=12, font=(fam, 13))
    style.configure("Hint.TLabel", foreground=COLOR_MUTED, font=(fam, 9))


class HistoryItemFrame(ttk.Frame):
    """다운로드 한 건(영상 1개)을 표시하는 카드.
    상태: preparing → downloading → merging → done / error."""

    THUMB_W = 132
    THUMB_H = 74   # 16:9

    def __init__(self, parent: tk.Misc, vid: str, title: str):
        super().__init__(parent)
        self.vid = vid
        self.file_path: str | None = None
        self._photo = None  # PhotoImage 참조 유지 (GC 방지)

        self.columnconfigure(1, weight=1)

        # 좌측: 썸네일 자리 (Canvas 로 픽셀 박스 + 이미지 호스팅)
        self.thumb_canvas = tk.Canvas(
            self, width=self.THUMB_W, height=self.THUMB_H,
            bg="#dcdcdc", highlightthickness=0,
        )
        self.thumb_canvas.grid(row=0, column=0, rowspan=2, padx=(10, 14), pady=10)

        # 가운데: 제목 + 상태 메타
        fam = preferred_font_family()
        self.title_lbl = ttk.Label(
            self, text=title or "(분석 중...)",
            font=(fam, 10, "bold"),
            wraplength=420, justify="left", anchor="w",
        )
        self.title_lbl.grid(row=0, column=1, sticky="ew", pady=(12, 0), padx=(0, 12))

        self.meta_lbl = ttk.Label(self, text="대기 중...", foreground=COLOR_MUTED)
        self.meta_lbl.grid(row=1, column=1, sticky="w", pady=(2, 12), padx=(0, 12))

        # 우측: 진행률 또는 액션 버튼
        self.progress = ttk.Progressbar(
            self, mode="determinate", maximum=100, length=140,
        )
        # 처음엔 진행률 표시 (다운로드 중)
        self.progress.grid(row=0, column=2, rowspan=2, padx=(0, 14), sticky="e")

        self.action_frame = ttk.Frame(self)
        # 완료/실패 시에만 grid

    # ---------- 외부 호출 ----------

    def set_thumbnail(self, photo) -> None:
        self._photo = photo
        self.thumb_canvas.delete("all")
        self.thumb_canvas.create_image(self.THUMB_W // 2, self.THUMB_H // 2, image=photo)

    def set_title(self, title: str) -> None:
        if title:
            self.title_lbl.configure(text=title)

    def set_downloading(self, pct: float, done: int, total: int, speed: float) -> None:
        self.progress.configure(value=pct)
        total_mb = total / 1024 / 1024 if total else 0
        done_mb = done / 1024 / 1024
        spd_mb = speed / 1024 / 1024 if speed else 0
        self.meta_lbl.configure(
            text=f"⏳ {pct:.0f}%  ·  {done_mb:.1f} / {total_mb:.1f} MB  ·  {spd_mb:.2f} MB/s"
        )

    def set_merging(self, file_path: str) -> None:
        self.file_path = file_path
        self.progress.configure(value=99)
        self.meta_lbl.configure(text="🔄 영상+음성 합치는 중...")

    def set_done(self) -> None:
        self.progress.grid_forget()
        size_text = ""
        if self.file_path:
            try:
                size_mb = Path(self.file_path).stat().st_size / 1024 / 1024
                size_text = f"  ·  {size_mb:.1f} MB"
            except OSError:
                pass
            self.meta_lbl.configure(
                text=f"✓ 완료{size_text}  ·  {Path(self.file_path).name}",
                foreground=COLOR_SUCCESS,
            )
        else:
            self.meta_lbl.configure(text="✓ 완료", foreground=COLOR_SUCCESS)

        # 액션 버튼
        for child in self.action_frame.winfo_children():
            child.destroy()
        ttk.Button(self.action_frame, text="폴더 열기",
                   command=self._open_in_folder).pack(side="top", pady=(0, 4))
        ttk.Button(self.action_frame, text="재생",
                   command=self._play_file).pack(side="top")
        self.action_frame.grid(row=0, column=2, rowspan=2, padx=(0, 14), sticky="e")

    def set_error(self, err: str) -> None:
        self.progress.grid_forget()
        short = (err or "알 수 없는 오류").splitlines()[-1][:120]
        self.meta_lbl.configure(text=f"✗ 실패: {short}", foreground=COLOR_DANGER)

    def set_cancelled(self) -> None:
        self.progress.grid_forget()
        self.meta_lbl.configure(text="■ 취소됨", foreground=COLOR_MUTED)

    # ---------- 내부 ----------

    def _open_in_folder(self) -> None:
        if not self.file_path:
            return
        try:
            # 파일을 선택한 상태로 탐색기 열기
            subprocess.run(
                ["explorer", f"/select,{self.file_path}"], check=False,
            )
        except Exception:
            try:
                os.startfile(Path(self.file_path).parent)
            except OSError:
                pass

    def _play_file(self) -> None:
        if self.file_path and Path(self.file_path).exists():
            try:
                os.startfile(self.file_path)
            except OSError:
                pass


class FfmpegInstallDialog(tk.Toplevel):
    """ffmpeg 다운로드/설치 진행률 모달.
    다운로드 중에는 닫을 수 없도록 잠그고, 완료/실패 후에만 닫기 허용."""

    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("초기 설정")
        self.geometry("460x210")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close_attempt)
        self._can_close = False

        fam = preferred_font_family()
        ttk.Label(self, text="동작에 필요한 파일 업데이트 중",
                  font=(fam, 14, "bold")).pack(pady=(22, 4))
        self.msg_var = tk.StringVar(value="잠시만 기다려 주세요...")
        ttk.Label(self, textvariable=self.msg_var,
                  foreground=COLOR_MUTED).pack(pady=(0, 14))

        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=28)

        self.detail_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.detail_var,
                  foreground=COLOR_MUTED).pack(pady=(10, 0))

        self.close_btn = ttk.Button(self, text="닫기", command=self.destroy)
        # 에러/완료 시에만 pack

    def _on_close_attempt(self) -> None:
        if self._can_close:
            self.destroy()

    def update_progress(self, p: FfmpegProgress) -> None:
        if p.stage == "downloading":
            self.msg_var.set("필요한 파일을 받고 있습니다...")
            self.progress.configure(value=p.pct)
            self.detail_var.set(p.message)
        elif p.stage == "extracting":
            self.msg_var.set("설치 중...")
            self.progress.configure(value=99)
            self.detail_var.set("거의 다 됐습니다.")
        elif p.stage == "done":
            self.msg_var.set("✓ 준비 완료")
            self.progress.configure(value=100)
            self.detail_var.set("")
            self._can_close = True
            self.after(800, self.destroy)
        elif p.stage == "error":
            self.msg_var.set("✗ 업데이트 실패")
            self.detail_var.set(p.message)
            self._can_close = True
            self.close_btn.pack(pady=(12, 0))


class DownloaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_TITLE} v{APP_VERSION}")
        self.root.geometry("760x880")
        self.root.minsize(680, 820)

        apply_theme(root)

        # 서비스 인스턴스
        self.analyzer = VideoAnalyzer()
        self.downloader = Downloader(
            on_progress=self._on_progress,
            on_done=lambda ok, err: self.root.after(0, self._on_done, ok, err),
            on_item_done=lambda vid, fp: self.root.after(0, self._on_item_done, vid, fp),
        )
        self.updater = UpdateChecker()
        self.installer = FfmpegInstaller()
        self.settings = Settings()
        self._cfg = self.settings.load()   # 저장된 설정 (없으면 {})

        # 일일 다운로드 사용량 (날짜가 바뀌었으면 0 으로 리셋)
        self._usage_date = date.today().isoformat()
        self._usage_count = (int(self._cfg.get("usage_count", 0))
                             if self._cfg.get("usage_date") == self._usage_date
                             else 0)

        # 상태
        self.ffmpeg_dir = find_ffmpeg()
        self.is_downloading = False
        self.is_analyzing = False
        self.last_clipboard = ""
        self.last_analyzed_url = ""
        self.analyze_after_id: str | None = None
        self.clipboard_auto = tk.BooleanVar(
            value=bool(self._cfg.get("clipboard_auto", False)))
        self.update_decision: UpdateDecision | None = None
        self._info_visible = False
        self._progress_visible = False
        self._ffmpeg_prompt_handled = False

        # 다운로드 히스토리: 최신순으로 쌓이는 4K Downloader 식 카드 리스트
        self.history_widgets: dict[str, HistoryItemFrame] = {}
        self.history_order: list[str] = []   # vid 들, 새 항목이 앞에 insert(0, ...)
        self.current_dl_vid: str | None = None
        # 이번 다운로드 세션에서 개별 완료 처리된 vid 집합.
        # 플레이리스트/다중 항목에서 각 항목을 한 번씩만 완료 처리하기 위함.
        self.completed_dl_vids: set[str] = set()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        # 원격 정지(킬스위치): 이전에 받은 정지 신호가 있으면 즉시 종료(오프라인 포함).
        if self._enforce_sticky_kill():
            self.aborted = True
            self.root.destroy()
            return
        # 첫 실행(또는 약관 변경) 시 면책 동의. 거부하면 실행을 중단한다.
        if not self._require_tos():
            self.aborted = True
            self.root.destroy()
            return
        self.aborted = False
        self._tick_clipboard()
        self._check_update_async()
        # 앱 렌더링이 끝난 뒤 ffmpeg 설치 안내 (없을 때만)
        self.root.after(500, self._maybe_offer_ffmpeg)

    # ---------------- 빌드 ----------------

    def _build_ui(self) -> None:
        self.container = ttk.Frame(self.root)
        self.container.pack(fill="both", expand=True, padx=PAD_OUTER, pady=24)

        # 푸터/업데이트 알림 (아래쪽 고정)
        self.footer = ttk.Frame(self.container)
        self.footer.pack(side="bottom", fill="x", pady=(10, 0))
        if self.ffmpeg_dir:
            self.ffmpeg_lbl = ttk.Label(
                self.footer, text="●  준비 완료", foreground=COLOR_SUCCESS,
            )
        else:
            self.ffmpeg_lbl = ttk.Label(
                self.footer,
                text="●  추가 파일 다운로드 필요 — 클릭하여 진행",
                foreground=COLOR_DANGER, cursor="hand2",
            )
            self.ffmpeg_lbl.bind(
                "<Button-1>", lambda _e: self._prompt_install_ffmpeg(force=True),
            )
        self.ffmpeg_lbl.pack(side="left")
        ttk.Label(self.footer, text=f"v{APP_VERSION}", style="Subtitle.TLabel").pack(side="right")
        self.usage_lbl = ttk.Label(self.footer, text="", style="Subtitle.TLabel")
        self.usage_lbl.pack(side="right", padx=(0, 16))
        self._update_usage_label()

        self.update_label = ttk.Label(self.container, text="",
                                      foreground=COLOR_ACCENT,
                                      cursor="hand2", anchor="w", justify="left")
        self.update_label.pack(side="bottom", fill="x", pady=(0, 6))

        # 제휴 광고 배너 (외부 광고주처럼 표시) — 클릭 시 PROMO_URL 로 이동
        self.promo_lbl = tk.Label(
            self.container, text=PROMO_TEXT, fg=COLOR_ACCENT, bg="#eef4ff",
            cursor="hand2", anchor="center", padx=10, pady=8,
            font=(preferred_font_family(), 9),
        )
        self.promo_lbl.pack(side="bottom", fill="x", pady=(6, 0))
        self.promo_lbl.bind("<Button-1>", lambda _e: safe_open_url(PROMO_URL))

        # 헤더
        header = ttk.Frame(self.container)
        header.pack(side="top", fill="x", pady=(0, 18))
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="유튜브·인스타·비메오·틱톡 등 영상을 원하는 화질로 저장",
                  style="Subtitle.TLabel").pack(side="left", padx=(12, 0), pady=(8, 0))

        self._build_url_card()
        self._build_info_card()
        self._build_progress_card()

    def _build_url_card(self) -> None:
        card = ttk.LabelFrame(self.container, text="  영상 링크  ")
        card.pack(side="top", fill="x", pady=(0, 14))
        self.url_card = card

        inner = ttk.Frame(card)
        inner.pack(fill="x", padx=PAD_CARD, pady=16)
        inner.columnconfigure(0, weight=1)

        self.url_var = tk.StringVar()
        self.url_var.trace_add("write", self._on_url_changed)
        self.url_entry = ttk.Entry(inner, textvariable=self.url_var, style="Hero.TEntry")
        self.url_entry.grid(row=0, column=0, sticky="ew")

        btn_row = ttk.Frame(inner)
        btn_row.grid(row=0, column=1, padx=(10, 0))
        ttk.Button(btn_row, text="붙여넣기", command=self._paste).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="지우기",
                   command=lambda: self.url_var.set("")).pack(side="left")

        self.url_status_var = tk.StringVar(value="")
        ttk.Label(inner, textvariable=self.url_status_var,
                  style="CardMuted.TLabel",
                  anchor="w", wraplength=640, justify="left"
                  ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))

        ttk.Checkbutton(
            inner,
            text="클립보드 자동 감지 (URL 복사 시 자동 채움)",
            variable=self.clipboard_auto,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # 로그인 쿠키 (인스타 비공개·멤버십 등): 분석 전부터 고를 수 있도록 여기 둔다.
        cookie_row = ttk.Frame(inner)
        cookie_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(cookie_row, text="로그인 쿠키").pack(side="left")
        self.cookies_var = tk.StringVar(
            value=self._cfg.get("cookies", COOKIE_BROWSERS[0][0]))
        cookie_combo = ttk.Combobox(
            cookie_row, textvariable=self.cookies_var,
            values=[label for label, _ in COOKIE_BROWSERS],
            state="readonly", width=16,
        )
        cookie_combo.pack(side="left", padx=(10, 12))
        cookie_combo.bind("<<ComboboxSelected>>", self._on_cookie_changed)
        ttk.Label(
            cookie_row,
            text="인스타·멤버십 등 로그인 영상에 필요 (해당 브라우저는 닫아두세요)",
            style="Hint.TLabel", wraplength=380, justify="left",
        ).pack(side="left")

    def _build_info_card(self) -> None:
        card = ttk.LabelFrame(self.container, text="  영상 정보  ")
        self.info_card = card

        # 미리보기: 썸네일(좌) + 제목/메타(우) — 4K Downloader 식 사전 노출
        preview = ttk.Frame(card)
        preview.pack(fill="x", padx=PAD_CARD, pady=(16, 0))
        preview.columnconfigure(1, weight=1)

        self.info_thumb = tk.Canvas(preview, width=160, height=90,
                                    bg="#dcdcdc", highlightthickness=0)
        self.info_thumb.grid(row=0, column=0, rowspan=2, padx=(0, 14), sticky="n")
        self._info_photo = None   # PhotoImage 참조 유지 (GC 방지)

        self.info_title_lbl = ttk.Label(
            preview, text="", style="CardTitle.TLabel",
            wraplength=440, justify="left", anchor="w",
        )
        self.info_title_lbl.grid(row=0, column=1, sticky="ew", pady=(2, 4))

        self.info_meta_lbl = ttk.Label(
            preview, text="", style="CardMuted.TLabel",
            wraplength=440, justify="left", anchor="w",
        )
        self.info_meta_lbl.grid(row=1, column=1, sticky="nw")

        inner = ttk.Frame(card)
        inner.pack(fill="x", padx=PAD_CARD, pady=16)
        inner.columnconfigure(1, weight=1)
        inner.columnconfigure(3, weight=1)

        # 옵션
        ttk.Label(inner, text="화질").grid(row=2, column=0, sticky="w", pady=6)
        # 기본 화질: 프리미어 호환성 우선 → 1080p (FHD)
        self.quality_var = tk.StringVar(
            value=self._cfg.get("quality", "1080p (FHD)"))
        self.quality_combo = ttk.Combobox(
            inner, textvariable=self.quality_var,
            values=[label for label, _ in QUALITY_TABLE],
            state="readonly", width=18,
        )
        self.quality_combo.grid(row=2, column=1, sticky="ew", padx=(12, 24), pady=6)

        ttk.Label(inner, text="포맷").grid(row=2, column=2, sticky="w", pady=6)
        self.format_var = tk.StringVar(
            value=self._cfg.get("format", FORMAT_TABLE[0][0]))
        ttk.Combobox(
            inner, textvariable=self.format_var,
            values=[label for label, _ in FORMAT_TABLE],
            state="readonly", width=22,
        ).grid(row=2, column=3, sticky="ew", padx=(12, 0), pady=6)

        ttk.Label(inner, text="저장 위치").grid(row=3, column=0, sticky="w", pady=(12, 6))
        default_dir = str(Path.home() / "Downloads" / "싹싹김치 다운로드")
        self.save_dir_var = tk.StringVar(value=self._cfg.get("save_dir", default_dir))

        save_row = ttk.Frame(inner)
        save_row.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(12, 0), pady=(12, 6))
        save_row.columnconfigure(0, weight=1)
        ttk.Entry(save_row, textvariable=self.save_dir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(save_row, text="찾기", command=self._pick_folder).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(save_row, text="열기", command=self._open_folder).grid(row=0, column=2, padx=(6, 0))

        # 부가 옵션 (모던 다운로더 기능)
        self.embed_meta_var = tk.BooleanVar(
            value=bool(self._cfg.get("embed_metadata", True)))
        self.embed_subs_var = tk.BooleanVar(
            value=bool(self._cfg.get("embed_subs", False)))
        self.sponsorblock_var = tk.BooleanVar(
            value=bool(self._cfg.get("sponsorblock", False)))
        opt_row = ttk.Frame(inner)
        opt_row.grid(row=4, column=0, columnspan=4, sticky="w", pady=(16, 0))
        ttk.Checkbutton(opt_row, text="메타데이터·썸네일 삽입",
                        variable=self.embed_meta_var).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(opt_row, text="자막 포함 (ko/en)",
                        variable=self.embed_subs_var).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(opt_row, text="SponsorBlock 구간 제거",
                        variable=self.sponsorblock_var).pack(side="left")

        self.download_btn = ttk.Button(
            inner, text="다운로드 시작",
            style="Accent.TButton",
            command=self._start_download,
        )
        self.download_btn.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(20, 0), ipady=6)

        # 취소 버튼: 평소 숨김, 다운로드 중에만 표시
        self.cancel_btn = ttk.Button(inner, text="취소", command=self._cancel_download)
        self.cancel_btn.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        self.cancel_btn.grid_remove()

    def _build_progress_card(self) -> None:
        """다운로드 목록 카드 — 4K Downloader 식 항목 스택, 최신이 위."""
        card = ttk.LabelFrame(self.container, text="  다운로드 목록  ")
        self.progress_card = card

        inner = ttk.Frame(card)
        inner.pack(fill="both", expand=True, padx=PAD_CARD, pady=14)
        inner.columnconfigure(0, weight=1)
        inner.rowconfigure(0, weight=1)

        # 스크롤 가능 영역: Canvas + 내부 Frame (tkinter 표준 패턴)
        # height 로 최소 높이를 확보해 다른 카드가 커져도 목록이 찌부되지 않게 한다.
        self.history_canvas = tk.Canvas(
            inner, highlightthickness=0, bg=COLOR_LOG_BG, height=200,
        )
        self.history_canvas.grid(row=0, column=0, sticky="nsew")

        vscroll = ttk.Scrollbar(inner, orient="vertical",
                                command=self.history_canvas.yview)
        vscroll.grid(row=0, column=1, sticky="ns")
        self.history_canvas.configure(yscrollcommand=vscroll.set)

        self.history_inner = tk.Frame(self.history_canvas, bg=COLOR_LOG_BG)
        self.history_window = self.history_canvas.create_window(
            (0, 0), window=self.history_inner, anchor="nw",
        )

        def _on_inner_configure(_e: object) -> None:
            self.history_canvas.configure(scrollregion=self.history_canvas.bbox("all"))

        def _on_canvas_configure(e: tk.Event) -> None:
            self.history_canvas.itemconfigure(self.history_window, width=e.width)

        self.history_inner.bind("<Configure>", _on_inner_configure)
        self.history_canvas.bind("<Configure>", _on_canvas_configure)

        # 마우스휠 스크롤
        def _on_wheel(e: tk.Event) -> None:
            self.history_canvas.yview_scroll(int(-e.delta / 120), "units")
        self.history_canvas.bind("<MouseWheel>", _on_wheel)
        self.history_inner.bind("<MouseWheel>", _on_wheel)

        # 빈 상태 안내
        self.history_empty_lbl = tk.Label(
            self.history_inner,
            text="다운로드를 시작하면 여기에 영상이 쌓입니다.",
            fg=COLOR_MUTED, bg=COLOR_LOG_BG,
            font=(preferred_font_family(), 10),
        )
        self.history_empty_lbl.pack(pady=32)

    # ---------------- 카드 표시 제어 ----------------

    def _relayout(self) -> None:
        self.info_card.pack_forget()
        self.progress_card.pack_forget()
        if self._info_visible:
            self.info_card.pack(side="top", fill="x", pady=(0, 14), after=self.url_card)
        if self._progress_visible:
            anchor = self.info_card if self._info_visible else self.url_card
            self.progress_card.pack(side="top", fill="both", expand=True,
                                    pady=(0, 14), after=anchor)

    def _show_info(self, visible: bool) -> None:
        self._info_visible = visible
        self._relayout()

    def _show_progress(self, visible: bool) -> None:
        self._progress_visible = visible
        self._relayout()

    # ---------------- 헬퍼 ----------------

    def _paste(self) -> None:
        try:
            self.url_var.set(self.root.clipboard_get().strip())
        except tk.TclError:
            pass

    def _pick_folder(self) -> None:
        d = filedialog.askdirectory(initialdir=self.save_dir_var.get() or str(Path.home()))
        if d:
            self.save_dir_var.set(d)

    def _open_folder(self) -> None:
        d = self.save_dir_var.get()
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            os.startfile(d)
        except OSError:
            log.exception("open folder failed")
            messagebox.showerror(APP_TITLE, "폴더를 열 수 없습니다.")

    # ---------------- URL 분석 ----------------

    def _on_url_changed(self, *_: object) -> None:
        if self.analyze_after_id is not None:
            self.root.after_cancel(self.analyze_after_id)
        self.analyze_after_id = self.root.after(600, self._maybe_analyze)

    def _on_cookie_changed(self, _event: object | None = None) -> None:
        """쿠키 브라우저가 바뀌면 같은 URL 도 다시 분석한다 (로그인 영상 대비)."""
        self.last_analyzed_url = ""
        self._on_url_changed()

    def _selected_cookie_browser(self) -> str | None:
        """현재 선택된 쿠키 브라우저의 yt-dlp 이름 (없으면 None)."""
        return dict(COOKIE_BROWSERS).get(self.cookies_var.get())

    def _maybe_analyze(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            self.url_status_var.set("")
            self._show_info(False)
            self.last_analyzed_url = ""
            return
        if not URL_RE.match(url):
            self.url_status_var.set("⚠ 올바른 링크(http/https) 형식이 아닙니다.")
            self._show_info(False)
            return
        if url == self.last_analyzed_url or self.is_analyzing:
            return
        self.last_analyzed_url = url
        self.is_analyzing = True
        self.url_status_var.set("⏳ 영상 정보 분석 중...")
        threading.Thread(
            target=self._analyze_worker,
            args=(url, self._selected_cookie_browser()), daemon=True,
        ).start()

    def _analyze_worker(self, url: str, cookies_browser: str | None) -> None:
        try:
            result = self.analyzer.probe(url, cookies_browser)
            self.root.after(0, self._apply_analysis, url, result)
        except Exception as e:
            log.exception("analysis failed")
            self.root.after(0, self._apply_analysis_error, url, str(e))
        finally:
            self.is_analyzing = False

    def _apply_analysis(self, url: str, result: AnalysisResult) -> None:
        # URL이 다시 바뀐 뒤 늦게 도착한 결과는 버린다.
        if url != self.last_analyzed_url:
            return
        self.url_status_var.set("")
        self.info_title_lbl.configure(text=result.title)
        self.info_meta_lbl.configure(text=result.meta)

        # 썸네일 미리보기: 일단 플레이스홀더 → 있으면 백그라운드로 받아 교체
        self._set_info_thumb_placeholder()
        if result.thumbnail and PIL_AVAILABLE:
            threading.Thread(target=self._fetch_info_thumbnail,
                             args=(url, result.thumbnail), daemon=True).start()

        if result.heights:
            max_h = max(result.heights)
            labels = [label for label, h in QUALITY_TABLE
                      if h is None or h == -1 or h <= max_h]
            self.quality_combo.configure(values=labels)
            if self.quality_var.get() not in labels:
                # 1080p 가능하면 우선, 아니면 최고화질
                self.quality_var.set(
                    "1080p (FHD)" if "1080p (FHD)" in labels else "최고화질 (자동)"
                )
        else:
            self.quality_combo.configure(values=[label for label, _ in QUALITY_TABLE])

        self._show_info(True)

    def _apply_analysis_error(self, url: str, err: str) -> None:
        if url != self.last_analyzed_url:
            return
        short = err.splitlines()[-1][:120] if err else "알 수 없는 오류"
        self.url_status_var.set(f"⚠ 영상 정보를 가져오지 못했습니다: {short}")
        self._show_info(False)

    # ---------------- 썸네일 미리보기 ----------------

    def _set_info_thumb_placeholder(self) -> None:
        self._info_photo = None
        self.info_thumb.delete("all")
        self.info_thumb.create_text(
            80, 45, text="미리보기", fill="#888888",
            font=(preferred_font_family(), 9),
        )

    def _fetch_info_thumbnail(self, url: str, thumb_url: str) -> None:
        # 네트워크 + 디코딩만 스레드에서. ImageTk 생성은 메인 스레드로 위임한다
        # (Tkinter 객체는 메인 스레드에서만 생성 가능 — 안 그러면 frozen 에서 실패).
        try:
            req = urllib.request.Request(
                thumb_url, headers={"User-Agent": f"{APP_TITLE}/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read(3_000_000)
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((160, 90))
        except Exception as e:
            log.info("info thumbnail fetch failed: %s", e)
            return
        self.root.after(0, self._apply_info_thumbnail, url, img)

    def _apply_info_thumbnail(self, url: str, img: object) -> None:
        # URL 이 바뀐 뒤 늦게 도착한 썸네일은 버린다.
        if url != self.last_analyzed_url:
            return
        try:
            photo = ImageTk.PhotoImage(img)   # 메인 스레드에서 생성
        except Exception as e:
            log.info("info thumbnail render failed: %s", e)
            return
        self._info_photo = photo   # 참조 유지 (GC 방지)
        self.info_thumb.delete("all")
        self.info_thumb.create_image(80, 45, image=photo)

    def _tick_clipboard(self) -> None:
        if self.clipboard_auto.get() and not self.is_downloading:
            try:
                txt = self.root.clipboard_get().strip()
                if (txt and txt != self.last_clipboard
                        and URL_RE.match(txt) and VIDEO_HOST_RE.search(txt)):
                    self.last_clipboard = txt
                    self.url_var.set(txt)
            except tk.TclError:
                pass
        self.root.after(1000, self._tick_clipboard)

    # ---------------- 다운로드 ----------------

    def _start_download(self) -> None:
        if self.is_downloading:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning(APP_TITLE, "영상 링크(URL)를 입력하세요.")
            return
        if not URL_RE.match(url):
            if not messagebox.askyesno(
                APP_TITLE,
                "올바른 링크(http/https) 형식이 아닌 것 같습니다. 그래도 시도할까요?",
            ):
                return

        # 일일 한도 확인 (날짜가 바뀌었으면 먼저 리셋). 유료 플랜은 무제한.
        self._roll_usage_date()
        if not self._is_unlimited() and self._usage_count >= DAILY_DOWNLOAD_LIMIT:
            messagebox.showinfo(
                APP_TITLE,
                f"오늘 무료 다운로드 한도({DAILY_DOWNLOAD_LIMIT}개)를 모두 사용했습니다.\n"
                "무료 한도는 내일 자정에 다시 초기화됩니다.",
            )
            return

        save_dir = Path(self.save_dir_var.get())
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"저장 폴더를 만들 수 없습니다:\n{e}")
            return

        quality_label = self.quality_var.get()
        is_audio_only = "음성만" in quality_label
        height = dict(QUALITY_TABLE).get(quality_label)
        container = dict(FORMAT_TABLE).get(self.format_var.get(), "mp4")
        format_str = "bestaudio/best" if is_audio_only else build_format_selector(height, container)

        if not self.ffmpeg_dir and not is_audio_only:
            if not messagebox.askyesno(
                APP_TITLE,
                "ffmpeg가 없어 4K 영상과 음성을 합칠 수 없습니다.\n"
                "음성 없는 영상이 받아질 수 있습니다. 계속할까요?",
            ):
                return

        self._save_settings()   # 현재 선택을 기억 (크래시 대비해 시작 시점에 저장)

        self.is_downloading = True
        self.current_dl_vid = None
        self.completed_dl_vids = set()
        self.download_btn.configure(state="disabled", text="다운로드 중...")
        self.cancel_btn.configure(state="normal", text="취소")
        self.cancel_btn.grid()
        self._show_progress(True)
        log.info("download start url=%s format=%s", url, format_str)

        cfg = DownloadConfig(
            url=url, out_dir=str(save_dir),
            format_str=format_str, is_audio_only=is_audio_only,
            container=container, ffmpeg_dir=self.ffmpeg_dir,
            cookies_browser=self._selected_cookie_browser(),
            embed_metadata=self.embed_meta_var.get(),
            embed_subs=self.embed_subs_var.get(),
            sponsorblock=self.sponsorblock_var.get(),
        )
        threading.Thread(target=self.downloader.run, args=(cfg,), daemon=True).start()

    def _cancel_download(self) -> None:
        """진행 중인 다운로드 취소 요청. 다음 진행률 콜백 때 실제로 멈춘다."""
        if not self.is_downloading:
            return
        self.downloader.cancel()
        self.cancel_btn.configure(state="disabled", text="취소 중...")

    # ---------------- 설정 영속화 ----------------

    def _collect_settings(self) -> dict:
        """현재 UI 선택값을 config.json 에 저장할 dict 로 모은다."""
        return {
            "save_dir": self.save_dir_var.get(),
            "cookies": self.cookies_var.get(),
            "quality": self.quality_var.get(),
            "format": self.format_var.get(),
            "embed_metadata": self.embed_meta_var.get(),
            "embed_subs": self.embed_subs_var.get(),
            "sponsorblock": self.sponsorblock_var.get(),
            "clipboard_auto": self.clipboard_auto.get(),
            "usage_date": self._usage_date,
            "usage_count": self._usage_count,
            "tos_accepted": self._cfg.get("tos_accepted"),
            "killed": self._cfg.get("killed", False),
            "killed_message": self._cfg.get("killed_message", ""),
        }

    def _save_settings(self) -> None:
        self.settings.save(self._collect_settings())

    def _on_close(self) -> None:
        """창 닫기: 설정 저장 → 광고 1회 노출 → 종료."""
        self._save_settings()
        self._show_exit_promo()
        self.root.destroy()

    # ---------------- 약관 동의 / 광고 ----------------

    def _require_tos(self) -> bool:
        """첫 실행(또는 약관 변경) 시 면책 동의를 받는다. 동의=True, 거부=False.
        이미 현재 버전 약관에 동의했으면 묻지 않고 바로 통과."""
        if self._cfg.get("tos_accepted") == TOS_VERSION:
            return True

        dlg = tk.Toplevel(self.root)
        dlg.title(f"{APP_TITLE} · 이용약관 및 면책")
        dlg.geometry("560x470")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, True)   # 세로는 조절 가능(안전망)
        dlg.minsize(560, 360)
        result = {"ok": False}

        fam = preferred_font_family()
        ttk.Label(dlg, text="이용약관 및 면책 고지",
                  font=(fam, 14, "bold")).pack(side="top", pady=(18, 8))

        # 버튼 줄을 먼저 '아래'에 고정해야, 본문 Text 가 길어도 버튼이 안 밀린다.
        btns = ttk.Frame(dlg)
        btns.pack(side="bottom", fill="x", padx=18, pady=14)

        txt = tk.Text(dlg, wrap="word", relief="flat", bg=COLOR_LOG_BG,
                      padx=14, pady=10, font=(fam, 10), height=12)
        txt.insert("1.0", TOS_BODY)
        txt.configure(state="disabled")
        txt.pack(side="top", fill="both", expand=True, padx=18, pady=(0, 4))

        def accept() -> None:
            result["ok"] = True
            self._cfg["tos_accepted"] = TOS_VERSION
            dlg.destroy()

        def decline() -> None:
            result["ok"] = False
            dlg.destroy()

        ok_btn = ttk.Button(btns, text="동의함", style="Accent.TButton",
                            command=accept)
        ok_btn.pack(side="right")
        ttk.Button(btns, text="종료", command=decline).pack(side="right", padx=(0, 8))
        ok_btn.focus_set()
        dlg.bind("<Return>", lambda _e: accept())
        dlg.protocol("WM_DELETE_WINDOW", decline)

        self.root.wait_window(dlg)
        if result["ok"]:
            self._save_settings()   # 동의 기록 영속화
        return result["ok"]

    def _show_exit_promo(self) -> None:
        """종료 시 광고 1회 노출 (외부 광고주 배너처럼). 실패해도 종료를 막지 않음."""
        try:
            dlg = tk.Toplevel(self.root)
            dlg.title("이용해 주셔서 감사합니다")
            dlg.geometry("420x210")
            dlg.transient(self.root)
            dlg.grab_set()
            dlg.resizable(False, False)
            fam = preferred_font_family()
            ttk.Button(dlg, text="닫기", command=dlg.destroy).pack(
                side="bottom", pady=(4, 12))
            ttk.Label(dlg, text="이용해 주셔서 감사합니다!",
                      font=(fam, 13, "bold")).pack(side="top", pady=(22, 6))
            promo = tk.Label(dlg, text=PROMO_TEXT, fg=COLOR_ACCENT, bg="#eef4ff",
                             cursor="hand2", padx=10, pady=12, wraplength=360,
                             justify="center", font=(fam, 10))
            promo.pack(side="top", fill="x", padx=20, pady=8)
            promo.bind("<Button-1>", lambda _e: safe_open_url(PROMO_URL))
            dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
            self.root.wait_window(dlg)
        except tk.TclError:
            pass

    def _enforce_sticky_kill(self) -> bool:
        """이전에 받은 원격 정지(killswitch)가 있으면 강제 종료한다(오프라인 포함).
        온라인이면 재확인해 정지가 해제됐으면 통과시키고, 확인 불가(오프라인)면
        마지막 상태(정지)를 유지한다. 정지면 True 를 반환한다."""
        if not self._cfg.get("killed"):
            return False
        decision = self.updater.fetch_decision()   # None = 오프라인/미설정
        if decision is not None and decision.kind != "killswitch":
            # 원격에서 정지 해제됨 → 끈끈한 정지 플래그 제거하고 정상 실행
            self._cfg["killed"] = False
            self._cfg["killed_message"] = ""
            self._save_settings()
            return False
        if decision is not None and decision.kind == "killswitch":
            self._cfg["killed_message"] = decision.message
            self._save_settings()
        msg = self._cfg.get("killed_message") or "현재 사용이 중단되었습니다."
        messagebox.showerror(APP_TITLE, msg)
        return True

    # ---------------- 일일 다운로드 한도 ----------------

    def _is_unlimited(self) -> bool:
        """유료 라이선스 활성 시 무제한. 현재는 미연동이라 항상 False.
        추후 라이선스/구독 확인 결과로 교체할 단일 지점."""
        return False

    def _roll_usage_date(self) -> None:
        """앱이 자정을 넘겨 켜져 있어도 날짜가 바뀌면 카운트를 리셋한다."""
        today = date.today().isoformat()
        if today != self._usage_date:
            self._usage_date, self._usage_count = today, 0
            self._update_usage_label()

    def _remaining_today(self) -> int:
        return max(0, DAILY_DOWNLOAD_LIMIT - self._usage_count)

    def _update_usage_label(self) -> None:
        if self._is_unlimited():
            self.usage_lbl.configure(text="무제한 (유료 플랜)")
            return
        self.usage_lbl.configure(
            text=f"오늘 남은 다운로드 {self._remaining_today()} / {DAILY_DOWNLOAD_LIMIT}")

    def _register_completed_download(self) -> None:
        """완료된 영상 1개를 일일 사용량에 반영한다. 한도에 도달하면 진행 중인
        작업(플레이리스트 등)을 멈춰 한도를 초과하지 않게 한다."""
        if self._is_unlimited():
            return
        self._usage_count += 1
        self._update_usage_label()
        self._save_settings()
        if self._usage_count >= DAILY_DOWNLOAD_LIMIT and self.is_downloading:
            self.downloader.cancel()

    def _on_progress(self, d: dict) -> None:
        """yt-dlp progress hook. 모든 갱신은 UI 스레드(root.after)로 위임."""
        info = d.get("info_dict") or {}
        vid = str(info.get("id") or "")
        if not vid:
            return
        status = d.get("status")

        if status == "downloading":
            title = str(info.get("title") or "")
            thumb_url = str(info.get("thumbnail") or "")
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            pct = (done / total * 100) if total else 0
            self.root.after(
                0, self._update_history_progress,
                vid, title, thumb_url, pct, done, total, speed,
            )
        elif status == "finished":
            fname = d.get("filename", "")
            self.root.after(0, self._update_history_merging, vid, fname)

    def _on_item_done(self, vid: str, filepath: str | None = None) -> None:
        """항목(영상) 1개의 후처리가 끝났을 때 호출 (vid별 개별 완료 처리).
        플레이리스트에서 각 항목이 끝나는 즉시 완료 표시되도록 한다."""
        if not vid or vid in self.completed_dl_vids:
            return
        widget = self.history_widgets.get(vid)
        if widget:
            if filepath:
                # 병합/추출 후의 최종 경로로 갱신 (임시 조각 파일명 교체).
                widget.file_path = filepath
            widget.set_done()
            self.completed_dl_vids.add(vid)
            self._register_completed_download()

    def _on_done(self, ok: bool, err: str | None) -> None:
        """다운로드(전체 작업) 종료 콜백.

        성공 항목은 대부분 postprocessor hook(_on_item_done)에서 이미
        개별 완료 처리됐다. 여기서는 후처리 hook이 발생하지 않은 경우
        (예: 재인코딩/병합이 필요 없는 단일 스트림)를 대비해, 아직
        완료 처리되지 않은 항목들을 마저 완료 처리하는 안전망 역할을 한다.
        실패 시에는 마지막 진행 항목에 오류를 표시한다."""
        self.is_downloading = False
        self.download_btn.configure(state="normal", text="다운로드 시작")
        self.cancel_btn.grid_remove()

        cancelled = (err == DOWNLOAD_CANCELLED)
        if ok:
            # hook으로 누락된 항목까지 일괄 완료 처리 (단일/다중 모두 안전).
            for vid, widget in self.history_widgets.items():
                if vid not in self.completed_dl_vids:
                    widget.set_done()
                    self.completed_dl_vids.add(vid)
                    self._register_completed_download()
        else:
            vid = self.current_dl_vid
            widget = self.history_widgets.get(vid) if vid else None
            if widget and vid not in self.completed_dl_vids:
                widget.set_cancelled() if cancelled else widget.set_error(err or "")

        log.info("download done ok=%s cancelled=%s items=%d",
                 ok, cancelled, len(self.completed_dl_vids))
        if not ok and not cancelled:
            messagebox.showerror(APP_TITLE, f"다운로드 실패:\n{err}")

    # ---------------- 히스토리 항목 관리 ----------------

    def _update_history_progress(self, vid: str, title: str, thumb_url: str,
                                 pct: float, done: int, total: int, speed: float) -> None:
        widget = self.history_widgets.get(vid)
        if widget is None:
            widget = self._add_history_item(vid, title, thumb_url)
        elif title:
            widget.set_title(title)
        self.current_dl_vid = vid
        widget.set_downloading(pct, done, total, speed)

    def _update_history_merging(self, vid: str, fname: str) -> None:
        widget = self.history_widgets.get(vid)
        if widget:
            widget.set_merging(fname)

    def _add_history_item(self, vid: str, title: str, thumb_url: str) -> HistoryItemFrame:
        if self.history_empty_lbl.winfo_ismapped():
            self.history_empty_lbl.pack_forget()
        widget = HistoryItemFrame(self.history_inner, vid, title)
        self.history_widgets[vid] = widget
        self.history_order.insert(0, vid)
        self._relayout_history()
        if thumb_url and PIL_AVAILABLE:
            threading.Thread(
                target=self._fetch_thumbnail, args=(vid, thumb_url), daemon=True,
            ).start()
        return widget

    def _relayout_history(self) -> None:
        """history_order 의 순서대로 항목 다시 배치 (최신이 위)."""
        for child in self.history_inner.pack_slaves():
            if child is not self.history_empty_lbl:
                child.pack_forget()
        for vid in self.history_order:
            w = self.history_widgets.get(vid)
            if w:
                w.pack(side="top", fill="x", padx=4, pady=4)

    def _fetch_thumbnail(self, vid: str, url: str) -> None:
        # 네트워크 + 디코딩만 스레드에서. ImageTk 생성은 메인 스레드로 위임.
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": f"{APP_TITLE}/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read(2_000_000)
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((HistoryItemFrame.THUMB_W, HistoryItemFrame.THUMB_H * 3))
        except Exception as e:
            log.info("thumbnail fetch failed for %s: %s", vid, e)
            return
        self.root.after(0, self._apply_thumbnail, vid, img)

    def _apply_thumbnail(self, vid: str, img: object) -> None:
        widget = self.history_widgets.get(vid)
        if not widget:
            return
        try:
            photo = ImageTk.PhotoImage(img)   # 메인 스레드에서 생성
        except Exception as e:
            log.info("thumbnail render failed for %s: %s", vid, e)
            return
        widget.set_thumbnail(photo)

    # ---------------- 업데이트 ----------------

    def _check_update_async(self) -> None:
        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self) -> None:
        decision = self.updater.fetch_decision()
        if decision is None:
            return
        self.root.after(0, self._apply_update_decision, decision)

    def _apply_update_decision(self, d: UpdateDecision) -> None:
        self.update_decision = d
        # 정지가 아닌 결정을 받으면 끈끈한 정지 플래그를 해제한다(원격 해제 반영).
        if d.kind != "killswitch" and self._cfg.get("killed"):
            self._cfg["killed"] = False
            self._cfg["killed_message"] = ""
            self._save_settings()
        if d.kind == "killswitch":
            # 끈끈하게: config 에 기록해 다음 실행부터는 오프라인이어도 정지 유지.
            self._cfg["killed"] = True
            self._cfg["killed_message"] = d.message
            self._save_settings()
            messagebox.showerror(APP_TITLE, d.message)
            if d.download_url and messagebox.askyesno(APP_TITLE, "공지/다운로드 페이지를 열까요?"):
                safe_open_url(d.download_url)
            self.root.destroy()
            return
        if d.kind == "force_update":
            messagebox.showwarning(APP_TITLE, d.message + "\n필수 업데이트가 필요합니다.")
            safe_open_url(d.download_url)
            self.root.destroy()
            return
        if d.kind == "new_version":
            text = f"⬆  새 버전 v{d.latest_version} 사용 가능 — 클릭하여 다운로드"
            if d.release_notes:
                text += f"   ({d.release_notes})"
            self.update_label.configure(text=text)
            self.update_label.bind("<Button-1>", self._open_update_page)

    def _open_update_page(self, _event: object | None = None) -> None:
        if self.update_decision:
            safe_open_url(self.update_decision.download_url)

    # ---------------- ffmpeg 자동 설치 ----------------

    def _maybe_offer_ffmpeg(self) -> None:
        """앱 시작 직후 1회 호출. ffmpeg 없으면 설치 안내."""
        if self.ffmpeg_dir or self._ffmpeg_prompt_handled:
            return
        self._prompt_install_ffmpeg(force=False)

    def _prompt_install_ffmpeg(self, *, force: bool) -> None:
        """ffmpeg 가 없으면 자동으로 모달 띄우고 백그라운드 다운로드."""
        if self.ffmpeg_dir:
            return
        self._ffmpeg_prompt_handled = True
        dialog = FfmpegInstallDialog(self.root)
        threading.Thread(
            target=self._install_ffmpeg_worker, args=(dialog,), daemon=True,
        ).start()

    def _install_ffmpeg_worker(self, dialog: FfmpegInstallDialog) -> None:
        def cb(p: FfmpegProgress) -> None:
            self.root.after(0, dialog.update_progress, p)
            if p.stage == "done":
                self.root.after(0, self._on_ffmpeg_installed)
        self.installer.install(cb)

    def _on_ffmpeg_installed(self) -> None:
        self.ffmpeg_dir = find_ffmpeg()
        if not self.ffmpeg_dir:
            return
        self.ffmpeg_lbl.configure(
            text="●  준비 완료", foreground=COLOR_SUCCESS, cursor="",
        )
        self.ffmpeg_lbl.unbind("<Button-1>")
        log.info("ffmpeg auto-install complete: %s", self.ffmpeg_dir)


# ============================================================
#  엔트리포인트
# ============================================================

def main() -> None:
    log_file = setup_logging()
    log.info("starting %s v%s, log=%s", APP_TITLE, APP_VERSION, log_file)
    root = tk.Tk()
    try:
        app = DownloaderApp(root)
        if not getattr(app, "aborted", False):
            root.mainloop()
    except Exception:
        log.exception("fatal error in main loop")
        raise


if __name__ == "__main__":
    main()
