# -*- coding: utf-8 -*-
"""오프컷 다운로더 서비스/순수 로직 테스트 (네트워크·Tk 불필요).

실행:  python -m pytest -q
UI 계층(DownloaderApp)은 Tk 디스플레이가 필요해 제외하고, UI 비의존 서비스
계층과 순수 함수만 검증한다."""

from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

import main
from main import (
    Downloader, DownloadConfig, FfmpegInstaller, Settings,
    URL_RE, VIDEO_HOST_RE, DOWNLOAD_CANCELLED,
    build_format_selector, compare_versions, is_safe_http_url, parse_version,
)


# ----------------------------- 순수 함수 -----------------------------

def test_parse_and_compare_versions():
    assert parse_version("1.2.3") == (1, 2, 3)
    assert parse_version("bad.1") == (0, 1)
    assert compare_versions("1.0", "1.0.0") == 0      # 0 패딩 동일 취급
    assert compare_versions("1.2", "1.10") == -1      # 정수 비교(문자열 아님)
    assert compare_versions("2.0", "1.9") == 1


def test_http_user_agent_is_latin1_safe():
    # HTTP 헤더는 latin-1 만 허용 → UA 에 한글(APP_TITLE)이 들어가면 모든
    # urllib 요청이 UnicodeEncodeError 로 실패한다. ASCII 보장 회귀 테스트.
    main.HTTP_UA.encode("latin-1")          # 예외 나면 실패
    assert "싹" not in main.HTTP_UA


def test_looks_like_login_error():
    assert main.looks_like_login_error("ERROR: Sign in to confirm your age")
    assert main.looks_like_login_error("This video is private")
    assert main.looks_like_login_error("멤버십 전용: 로그인이 필요합니다")
    assert not main.looks_like_login_error("HTTP Error 404: Not Found")
    assert not main.looks_like_login_error("Unable to download webpage: timed out")


@pytest.mark.parametrize("url,ok", [
    ("https://youtube.com/watch?v=x", True),
    ("http://example.com", True),
    ("javascript:alert(1)", False),
    ("file:///etc/passwd", False),
    ("ftp://host/x", False),
    (None, False),
])
def test_is_safe_http_url(url, ok):
    assert is_safe_http_url(url) is ok


def test_build_format_selector_mp4_prefers_avc_aac():
    sel = build_format_selector(1080, "mp4")
    assert "height<=1080" in sel and "avc1" in sel and "mp4a" in sel


def test_build_format_selector_mkv_best():
    assert build_format_selector(None, "mkv") == "bestvideo+bestaudio/best"


# ----------------------------- URL 인식 -----------------------------

@pytest.mark.parametrize("url", [
    "https://vimeo.com/12345",
    "https://www.instagram.com/reel/abc/",
    "http://x.com/i/status/1",
])
def test_url_re_accepts_any_http(url):
    assert URL_RE.match(url)


def test_url_re_rejects_nonurl():
    assert not URL_RE.match("hello world")
    assert not URL_RE.match("vimeo.com/12345")     # 스킴 없음


def test_video_host_re_matches_known_only():
    assert VIDEO_HOST_RE.search("https://www.instagram.com/reel/abc/")
    assert VIDEO_HOST_RE.search("https://x.com/i/status/1")
    assert not VIDEO_HOST_RE.search("https://example.com/video")


# ------------------- Downloader._configure_output -------------------

def _opts_for(**kw):
    cfg = DownloadConfig(url="u", out_dir=".", format_str="best",
                         is_audio_only=kw.pop("audio", False),
                         container=kw.pop("container", "mp4"),
                         ffmpeg_dir=None, **kw)
    opts: dict = {}
    Downloader._configure_output(cfg, opts)
    return opts


def test_configure_output_video_all_options_order():
    o = _opts_for(embed_metadata=True, embed_subs=True, sponsorblock=True)
    keys = [p["key"] for p in o["postprocessors"]]
    # yt-dlp CLI 생성 순서와 동일해야 한다.
    assert keys == ["SponsorBlock", "FFmpegEmbedSubtitle", "ModifyChapters",
                    "FFmpegMetadata", "EmbedThumbnail"]
    # SponsorBlock categories 는 반드시 set 타입
    assert isinstance(o["postprocessors"][0]["categories"], set)
    assert o["writesubtitles"] and o["subtitleslangs"] == ["ko", "en"]
    assert o["writethumbnail"] and o["merge_output_format"] == "mp4"


def test_configure_output_audio():
    o = _opts_for(audio=True, embed_metadata=True)
    assert [p["key"] for p in o["postprocessors"]] == [
        "FFmpegExtractAudio", "FFmpegMetadata", "EmbedThumbnail"]
    assert "merge_output_format" not in o     # 음성은 병합 컨테이너 미사용


def test_configure_output_minimal_no_extras():
    o = _opts_for(embed_metadata=False, container="mkv")
    assert o["postprocessors"] == []
    assert o["merge_output_format"] == "mkv"
    assert "writethumbnail" not in o


# ----------------------------- 취소 / 훅 -----------------------------

def test_cancel_raises_in_progress_hook():
    seen = []
    dl = Downloader(on_progress=lambda d: seen.append(d),
                    on_done=lambda ok, e: None)
    dl._progress_hook({"status": "downloading"})   # 취소 전: 위임
    assert seen == [{"status": "downloading"}]
    dl.cancel()
    with pytest.raises(main.yt_dlp.utils.DownloadCancelled):
        dl._progress_hook({"status": "downloading"})


def test_postprocessor_hook_forwards_final_path():
    calls = []
    dl = Downloader(on_progress=lambda d: None, on_done=lambda ok, e: None,
                    on_item_done=lambda vid, fp: calls.append((vid, fp)))
    dl._postprocessor_hook({"status": "finished",
                            "info_dict": {"id": "abc", "filepath": "/out/a.mp4"}})
    dl._postprocessor_hook({"status": "started", "info_dict": {"id": "abc"}})
    assert calls == [("abc", "/out/a.mp4")]


def test_download_cancelled_sentinel_is_distinct():
    # 실제 에러 메시지와 충돌하지 않는 내부 신호값이어야 한다.
    assert DOWNLOAD_CANCELLED.startswith("__") and DOWNLOAD_CANCELLED.endswith("__")


# ----------------------------- Settings -----------------------------

def test_settings_round_trip(tmp_path):
    s = Settings(tmp_path / "config.json")
    assert s.load() == {}                       # 파일 없음 → 빈 dict
    s.save({"save_dir": "D:/x", "quality": "1080p (FHD)"})
    assert s.load()["save_dir"] == "D:/x"


def test_settings_corrupt_file_falls_back(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert Settings(p).load() == {}             # 손상 → 빈 dict (예외 X)


def test_settings_save_is_atomic_no_partial(tmp_path):
    p = tmp_path / "config.json"
    s = Settings(p)
    s.save({"a": 1})
    # .tmp 잔재가 남지 않아야 한다
    assert not (tmp_path / "config.json.tmp").exists()
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}


# ------------------- FfmpegInstaller 무결성 검증 -------------------

def _make_zip(path, member, content=b"data"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(member, content)


def test_verify_zip_rejects_too_small(tmp_path):
    p = tmp_path / "f.zip"
    _make_zip(p, "ffmpeg/bin/ffmpeg.exe")
    with pytest.raises(RuntimeError):           # 5MB 미만 → 비정상
        FfmpegInstaller._verify_zip(p)


def test_verify_zip_structure_and_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(FfmpegInstaller, "MIN_ZIP_BYTES", 0)
    monkeypatch.setattr(FfmpegInstaller, "MAX_ZIP_BYTES", 10**9)
    good = tmp_path / "good.zip"
    _make_zip(good, "ffmpeg-x/bin/ffmpeg.exe")
    FfmpegInstaller._verify_zip(good)           # 구조 OK → 통과

    digest = FfmpegInstaller._sha256(good)
    FfmpegInstaller._verify_zip(good, digest)   # 올바른 해시 → 통과
    with pytest.raises(RuntimeError):
        FfmpegInstaller._verify_zip(good, "deadbeef")  # 해시 불일치 → 예외


def test_verify_zip_rejects_wrong_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(FfmpegInstaller, "MIN_ZIP_BYTES", 0)
    monkeypatch.setattr(FfmpegInstaller, "MAX_ZIP_BYTES", 10**9)
    bad = tmp_path / "bad.zip"
    _make_zip(bad, "some/other/file.txt")
    with pytest.raises(RuntimeError):           # ffmpeg.exe 없음
        FfmpegInstaller._verify_zip(bad)


def test_fetch_expected_digest_parses_api(monkeypatch):
    payload = json.dumps({"assets": [
        {"name": "other.zip", "digest": "sha256:ffff"},
        {"name": FfmpegInstaller.ASSET_NAME, "digest": "sha256:ABC123"},
    ]}).encode("utf-8")

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return payload

    monkeypatch.setattr(main.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp())
    assert FfmpegInstaller._fetch_expected_digest() == "abc123"   # 소문자 hex


def test_fetch_expected_digest_network_error_returns_none(monkeypatch):
    def boom(req, timeout=0):
        raise OSError("offline")
    monkeypatch.setattr(main.urllib.request, "urlopen", boom)
    assert FfmpegInstaller._fetch_expected_digest() is None       # 안전 폴백
