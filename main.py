# -*- coding: utf-8 -*-
"""오프컷 유튜브 다운로더 - 4K/8K 지원 GUI

구조
----
- 상수/유틸     : 앱 메타데이터, 디자인 토큰, 순수 함수
- 서비스 계층  : VideoAnalyzer / Downloader / UpdateChecker (UI 비의존, 테스트 가능)
- UI 계층      : DownloaderApp (Tk root + 카드 단위 _build_xxx + 이벤트 핸들러)

사이드 이펙트는 서비스 클래스에 격리하고, UI는 콜백을 통해 결과만 받는다.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import shutil
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
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from typing import Callable

import yt_dlp


# ============================================================
#  메타데이터 / 설정
# ============================================================

APP_TITLE = "오프컷 유튜브 다운로더"
APP_VERSION = "1.0"
APP_ID = "OffcutYoutubeDownloader"  # 로그 폴더 등에 사용

# 깃 저장소를 만든 뒤 아래 URL의 <your-account>/<your-repo> 두 자리만 바꿔주세요.
VERSION_INFO_URL = "https://raw.githubusercontent.com/rlagyqls051-create/offcut-youtube-downloader/main/version.json"
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
    ("MKV (최고화질·무손실)", "mkv"),
    ("MP4 (호환성 우선)",    "mp4"),
]

# 유튜브 URL 인식. youtube.com/watch, youtu.be, shorts, live, embed, music 까지 커버.
YT_URL_RE = re.compile(
    r"^(https?://)?(www\.|m\.|music\.)?"
    r"(youtube\.com/(watch\?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"[\w\-]+",
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


log = logging.getLogger("offcut-yt")


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
    meta: str                 # 한 줄 요약
    heights: list[int] = field(default_factory=list)
    is_playlist: bool = False


class VideoAnalyzer:
    """yt-dlp로 메타데이터만 가져온다 (다운로드 X). UI 비의존."""

    def probe(self, url: str) -> AnalysisResult:
        with yt_dlp.YoutubeDL(
            {"quiet": True, "no_warnings": True, "skip_download": True}
        ) as ydl:
            info = ydl.extract_info(url, download=False)

        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            first = next((e for e in entries if e), None)
            title = info.get("title", "(제목 없음)")
            meta = f"📃 플레이리스트  ·  영상 {len(entries)}개 (전체 일괄 다운로드)"
            return AnalysisResult(
                title=title, meta=meta,
                heights=self._heights(first or {}),
                is_playlist=True,
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
        return AnalysisResult(title=title, meta="  ·  ".join(parts), heights=heights)

    @staticmethod
    def _heights(info: dict) -> list[int]:
        formats = info.get("formats") or []
        hs = {f.get("height") for f in formats if isinstance(f.get("height"), int)}
        return sorted(hs)


# ============================================================
#  서비스: 다운로드
# ============================================================

@dataclass
class DownloadConfig:
    url: str
    out_dir: str
    format_str: str
    is_audio_only: bool
    container: str
    ffmpeg_dir: str | None


class Downloader:
    """yt-dlp 다운로드 래퍼. progress/완료 콜백 호출."""

    def __init__(self,
                 on_progress: Callable[[dict], None],
                 on_done: Callable[[bool, str | None], None]):
        self._on_progress = on_progress
        self._on_done = on_done

    def run(self, cfg: DownloadConfig) -> None:
        opts: dict = {
            "outtmpl": os.path.join(cfg.out_dir, "%(title)s [%(id)s].%(ext)s"),
            "format": cfg.format_str,
            "noplaylist": False,
            "progress_hooks": [self._on_progress],
            "quiet": True,
            "no_warnings": True,
            "concurrent_fragment_downloads": 4,
        }
        if cfg.ffmpeg_dir:
            opts["ffmpeg_location"] = cfg.ffmpeg_dir
        if cfg.is_audio_only:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            opts["merge_output_format"] = cfg.container

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([cfg.url])
            self._on_done(True, None)
        except Exception as e:
            log.exception("download failed")
            self._on_done(False, str(e))


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
        current = parse_version(self.current_version)

        if data.get("killswitch"):
            return UpdateDecision(
                kind="killswitch",
                message=str(data.get("killswitch_message")
                            or "현재 사용이 일시 중단되었습니다."),
                download_url=safe_url,
            )

        min_ver = parse_version(str(data.get("min_supported_version", "0")))
        if current < min_ver:
            return UpdateDecision(
                kind="force_update",
                message=f"현재 버전(v{self.current_version})은 더 이상 지원되지 않습니다.",
                download_url=safe_url,
            )

        latest_str = str(data.get("latest_version", self.current_version))
        if current < parse_version(latest_str):
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
    """BtbN/FFmpeg-Builds latest 빌드를 받아 LOCALAPPDATA 에 캐시."""

    URL = (
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
        "ffmpeg-master-latest-win64-gpl.zip"
    )
    DOWNLOAD_TIMEOUT = 60  # 초

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
        self.root.geometry("740x780")
        self.root.minsize(660, 700)

        apply_theme(root)

        # 서비스 인스턴스
        self.analyzer = VideoAnalyzer()
        self.downloader = Downloader(
            on_progress=self._on_progress,
            on_done=lambda ok, err: self.root.after(0, self._on_done, ok, err),
        )
        self.updater = UpdateChecker()
        self.installer = FfmpegInstaller()

        # 상태
        self.ffmpeg_dir = find_ffmpeg()
        self.is_downloading = False
        self.is_analyzing = False
        self.last_clipboard = ""
        self.last_analyzed_url = ""
        self.analyze_after_id: str | None = None
        self.clipboard_auto = tk.BooleanVar(value=False)
        self.update_decision: UpdateDecision | None = None
        self._info_visible = False
        self._progress_visible = False
        self._ffmpeg_prompt_handled = False

        self._build_ui()
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

        self.update_label = ttk.Label(self.container, text="",
                                      foreground=COLOR_ACCENT,
                                      cursor="hand2", anchor="w", justify="left")
        self.update_label.pack(side="bottom", fill="x", pady=(0, 6))

        # 헤더
        header = ttk.Frame(self.container)
        header.pack(side="top", fill="x", pady=(0, 18))
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="유튜브 영상을 원하는 화질로 저장",
                  style="Subtitle.TLabel").pack(side="left", padx=(12, 0), pady=(8, 0))

        self._build_url_card()
        self._build_info_card()
        self._build_progress_card()

    def _build_url_card(self) -> None:
        card = ttk.LabelFrame(self.container, text="  유튜브 링크  ")
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

    def _build_info_card(self) -> None:
        card = ttk.LabelFrame(self.container, text="  영상 정보  ")
        self.info_card = card

        inner = ttk.Frame(card)
        inner.pack(fill="x", padx=PAD_CARD, pady=16)
        inner.columnconfigure(1, weight=1)
        inner.columnconfigure(3, weight=1)

        self.info_title_lbl = ttk.Label(
            inner, text="", style="CardTitle.TLabel",
            wraplength=640, justify="left", anchor="w",
        )
        self.info_title_lbl.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 4))

        self.info_meta_lbl = ttk.Label(inner, text="", style="CardMuted.TLabel")
        self.info_meta_lbl.grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 16))

        # 옵션
        ttk.Label(inner, text="화질").grid(row=2, column=0, sticky="w", pady=6)
        self.quality_var = tk.StringVar(value="최고화질 (자동)")
        self.quality_combo = ttk.Combobox(
            inner, textvariable=self.quality_var,
            values=[label for label, _ in QUALITY_TABLE],
            state="readonly", width=18,
        )
        self.quality_combo.grid(row=2, column=1, sticky="ew", padx=(12, 24), pady=6)

        ttk.Label(inner, text="포맷").grid(row=2, column=2, sticky="w", pady=6)
        self.format_var = tk.StringVar(value=FORMAT_TABLE[0][0])
        ttk.Combobox(
            inner, textvariable=self.format_var,
            values=[label for label, _ in FORMAT_TABLE],
            state="readonly", width=22,
        ).grid(row=2, column=3, sticky="ew", padx=(12, 0), pady=6)

        ttk.Label(inner, text="저장 위치").grid(row=3, column=0, sticky="w", pady=(12, 6))
        default_dir = str(Path.home() / "Downloads" / "유튜브")
        self.save_dir_var = tk.StringVar(value=default_dir)

        save_row = ttk.Frame(inner)
        save_row.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(12, 0), pady=(12, 6))
        save_row.columnconfigure(0, weight=1)
        ttk.Entry(save_row, textvariable=self.save_dir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(save_row, text="찾기", command=self._pick_folder).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(save_row, text="열기", command=self._open_folder).grid(row=0, column=2, padx=(6, 0))

        self.download_btn = ttk.Button(
            inner, text="다운로드 시작",
            style="Accent.TButton",
            command=self._start_download,
        )
        self.download_btn.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(20, 0), ipady=6)

    def _build_progress_card(self) -> None:
        card = ttk.LabelFrame(self.container, text="  진행 상황  ")
        self.progress_card = card

        inner = ttk.Frame(card)
        inner.pack(fill="both", expand=True, padx=PAD_CARD, pady=16)
        inner.columnconfigure(0, weight=1)
        inner.rowconfigure(2, weight=1)

        self.progress = ttk.Progressbar(inner, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.status_var = tk.StringVar(value="대기 중")
        ttk.Label(inner, textvariable=self.status_var,
                  style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 10))

        log_wrap = ttk.Frame(inner)
        log_wrap.grid(row=2, column=0, sticky="nsew")
        self.log_view = tk.Text(
            log_wrap, height=6, wrap="word", state="disabled",
            bg=COLOR_LOG_BG, fg="#1c1c1c", relief="flat",
            font=("Consolas", 9), padx=10, pady=8,
            borderwidth=0, highlightthickness=1,
            highlightbackground=COLOR_LOG_BORDER,
        )
        scroll = ttk.Scrollbar(log_wrap, command=self.log_view.yview)
        self.log_view.configure(yscrollcommand=scroll.set)
        self.log_view.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

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

    def _append_log(self, msg: str) -> None:
        self.log_view.configure(state="normal")
        self.log_view.insert("end", msg + "\n")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    def _set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    # ---------------- URL 분석 ----------------

    def _on_url_changed(self, *_: object) -> None:
        if self.analyze_after_id is not None:
            self.root.after_cancel(self.analyze_after_id)
        self.analyze_after_id = self.root.after(600, self._maybe_analyze)

    def _maybe_analyze(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            self.url_status_var.set("")
            self._show_info(False)
            self.last_analyzed_url = ""
            return
        if not YT_URL_RE.match(url):
            self.url_status_var.set("⚠ 유튜브 URL 형식이 아닙니다.")
            self._show_info(False)
            return
        if url == self.last_analyzed_url or self.is_analyzing:
            return
        self.last_analyzed_url = url
        self.is_analyzing = True
        self.url_status_var.set("⏳ 영상 정보 분석 중...")
        threading.Thread(target=self._analyze_worker, args=(url,), daemon=True).start()

    def _analyze_worker(self, url: str) -> None:
        try:
            result = self.analyzer.probe(url)
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

        if result.heights:
            max_h = max(result.heights)
            labels = [label for label, h in QUALITY_TABLE
                      if h is None or h == -1 or h <= max_h]
            self.quality_combo.configure(values=labels)
            if self.quality_var.get() not in labels:
                self.quality_var.set("최고화질 (자동)")
        else:
            self.quality_combo.configure(values=[label for label, _ in QUALITY_TABLE])

        self._show_info(True)

    def _apply_analysis_error(self, url: str, err: str) -> None:
        if url != self.last_analyzed_url:
            return
        short = err.splitlines()[-1][:120] if err else "알 수 없는 오류"
        self.url_status_var.set(f"⚠ 영상 정보를 가져오지 못했습니다: {short}")
        self._show_info(False)

    def _tick_clipboard(self) -> None:
        if self.clipboard_auto.get() and not self.is_downloading:
            try:
                txt = self.root.clipboard_get().strip()
                if txt and txt != self.last_clipboard and YT_URL_RE.match(txt):
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
            messagebox.showwarning(APP_TITLE, "유튜브 URL을 입력하세요.")
            return
        if not YT_URL_RE.match(url):
            if not messagebox.askyesno(
                APP_TITLE,
                "유튜브 URL 형식이 아닌 것 같습니다. 그래도 시도할까요?",
            ):
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
        container = dict(FORMAT_TABLE).get(self.format_var.get(), "mkv")
        format_str = "bestaudio/best" if is_audio_only else build_format_selector(height, container)

        if not self.ffmpeg_dir and not is_audio_only:
            if not messagebox.askyesno(
                APP_TITLE,
                "ffmpeg가 없어 4K 영상과 음성을 합칠 수 없습니다.\n"
                "음성 없는 영상이 받아질 수 있습니다. 계속할까요?",
            ):
                return

        self.is_downloading = True
        self.download_btn.configure(state="disabled", text="다운로드 중...")
        self._show_progress(True)
        self.progress.configure(value=0)
        self._set_status("준비 중...")
        self._append_log(f"--- 시작: {url} ---")
        self._append_log(f"  포맷: {format_str}")
        log.info("download start url=%s format=%s", url, format_str)

        cfg = DownloadConfig(
            url=url, out_dir=str(save_dir),
            format_str=format_str, is_audio_only=is_audio_only,
            container=container, ffmpeg_dir=self.ffmpeg_dir,
        )
        threading.Thread(target=self.downloader.run, args=(cfg,), daemon=True).start()

    def _on_progress(self, d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            pct = (done / total * 100) if total else 0

            def _update() -> None:
                self.progress.configure(value=pct)
                self._set_status(
                    f"{pct:5.1f}%  ·  {done/1024/1024:.1f} / {total/1024/1024:.1f} MB"
                    f"  ·  {speed/1024/1024:.2f} MB/s  ·  남은 시간 {int(eta)}s"
                )
            self.root.after(0, _update)
        elif status == "finished":
            fname = d.get("filename", "")
            self.root.after(0, self._append_log, f"[완료] {Path(fname).name}")
            self.root.after(0, self._set_status, "후처리 중 (영상+음성 합치기)...")

    def _on_done(self, ok: bool, err: str | None) -> None:
        self.is_downloading = False
        self.download_btn.configure(state="normal", text="다운로드 시작")
        if ok:
            self.progress.configure(value=100)
            self._set_status("✓ 다운로드 완료")
            self._append_log("--- 모두 완료 ---\n")
            log.info("download done")
            messagebox.showinfo(APP_TITLE, "다운로드가 완료되었습니다!")
        else:
            self._set_status("✗ 오류 발생")
            self._append_log(f"[오류] {err}\n")
            messagebox.showerror(APP_TITLE, f"다운로드 실패:\n{err}")

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
        if d.kind == "killswitch":
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
        DownloaderApp(root)
        root.mainloop()
    except Exception:
        log.exception("fatal error in main loop")
        raise


if __name__ == "__main__":
    main()
