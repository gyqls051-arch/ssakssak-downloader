# -*- coding: utf-8 -*-
"""싹싹김치 다운로더 서비스/순수 로직 테스트 (네트워크·GUI 불필요).

실행:  python -m pytest -q
UI 계층(pywebview/web_ui)은 제외하고, UI 비의존 서비스 계층과 순수 함수만
검증한다."""

from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

import main
from main import (
    Downloader, DownloadConfig, FfmpegInstaller, Settings,
    URL_RE, DOWNLOAD_CANCELLED,
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


# ------------------- Downloader._configure_output -------------------

def _opts_for(**kw):
    cfg = DownloadConfig(url="u", out_dir=".", format_str="best",
                         is_audio_only=kw.pop("audio", False),
                         container=kw.pop("container", "mp4"),
                         ffmpeg_dir=None, **kw)
    opts: dict = {}
    Downloader._configure_output(cfg, opts)
    return opts


def test_configure_output_video_metadata():
    o = _opts_for(embed_metadata=True)
    keys = [p["key"] for p in o["postprocessors"]]
    # 영상: 메타데이터 → 썸네일 순 (yt-dlp CLI 순서와 동일)
    assert keys == ["FFmpegMetadata", "EmbedThumbnail"]
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


# ----------------------- DRM 감지 -----------------------

def test_looks_like_drm():
    assert main.looks_like_drm("ERROR: This video is DRM protected")
    assert main.looks_like_drm("Widevine license required")
    assert main.looks_like_drm("Content uses PlayReady")
    assert not main.looks_like_drm("HTTP Error 403: Forbidden")
    assert not main.looks_like_drm("Sign in to confirm your age")


# ------------------- 매니페스트(HLS/DASH) 스캔 -------------------

def test_manifest_re_finds_hls_and_dash():
    html = (
        '<video><source src="https://cdn.x.com/a/master.m3u8?token=1"></video>'
        ' 그리고 https://cdn.x.com/b/manifest.mpd 도 있음'
    )
    found = main.MANIFEST_RE.findall(html)
    assert any(".m3u8" in u for u in found)
    assert any(".mpd" in u for u in found)


def test_manifest_re_ignores_plain_media():
    assert main.MANIFEST_RE.findall("https://x.com/video.mp4 https://x.com/a.jpg") == []


# ------------------- 폴백 헤더 / 시도 계획 -------------------

def test_headers_for_fills_referer_and_origin():
    h = Downloader._headers_for("https://site.com/watch?v=1")
    assert h["User-Agent"] == main.BROWSER_UA
    assert h["Origin"] == "https://site.com"
    assert h["Referer"] == "https://site.com/"


def _dl():
    return Downloader(on_progress=lambda d: None, on_done=lambda ok, e: None)


def _cfg(**kw):
    return DownloadConfig(
        url=kw.pop("url", "https://s.com/v"), out_dir=kw.pop("out_dir", "."),
        format_str=kw.pop("format_str", "FMT"),
        is_audio_only=kw.pop("is_audio_only", False),
        container="mp4", ffmpeg_dir=None, **kw)


def test_attempt_plan_escalates_for_video(monkeypatch):
    monkeypatch.setattr(main, "detect_cookie_browser", lambda: None)
    labels = [lbl for lbl, _ in _dl()._attempt_plan(_cfg())]
    assert labels[0] == "기본 다운로드"
    assert any("헤더" in l for l in labels)
    assert any("호환 포맷" in l for l in labels)
    assert any("최저 화질" in l for l in labels)


def test_attempt_plan_audio_skips_format_downgrade(monkeypatch):
    monkeypatch.setattr(main, "detect_cookie_browser", lambda: None)
    labels = [lbl for lbl, _ in _dl()._attempt_plan(
        _cfg(is_audio_only=True, format_str="bestaudio/best"))]
    assert not any("화질" in l for l in labels)        # 음성은 포맷 낮춤 없음


def test_attempt_plan_adds_cookie_step_when_browser_present(monkeypatch):
    monkeypatch.setattr(main, "detect_cookie_browser", lambda: "chrome")
    plan = _dl()._attempt_plan(_cfg())
    assert any("쿠키" in lbl for lbl, _ in plan)
    cookie_extra = next(extra for lbl, extra in plan if "쿠키" in lbl)
    assert cookie_extra["cookiesfrombrowser"] == ("chrome", None, None, None)


def test_base_opts_has_socket_timeout_and_hooks():
    opts = _dl()._base_opts(_cfg())
    assert opts["socket_timeout"] == main.SOCKET_TIMEOUT
    assert opts["retries"] == 10 and opts["fragment_retries"] == 10
    assert "format" not in opts                         # 포맷은 시도별로 덮음


def test_scan_manifest_prefers_hls(monkeypatch):
    html = (b'<html>x = "https:\\/\\/c.com\\/v\\/index.mpd"; '
            b'y = "https://c.com/v/master.m3u8?a=1";</html>')

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return html

    monkeypatch.setattr(main.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp())
    got = Downloader._scan_manifest("https://c.com/watch")
    assert got == "https://c.com/v/master.m3u8?a=1"     # .m3u8 우선 + \/ 복원


# ------------------- UpdateChecker 판정 -------------------

def _uc():
    return main.UpdateChecker(current_version="1.0")


def test_interpret_new_version_carries_notes_and_url():
    d = _uc()._interpret({"latest_version": "2.0",
                          "release_notes": "메모",
                          "download_url": "https://x.com/dl"})
    assert d.kind == "new_version"
    assert d.latest_version == "2.0" and d.release_notes == "메모"
    assert d.download_url == "https://x.com/dl"


def test_interpret_kinds():
    assert _uc()._interpret({"killswitch": True}).kind == "killswitch"
    assert _uc()._interpret({"min_supported_version": "1.5"}).kind == "force_update"
    assert _uc()._interpret({"latest_version": "1.0"}).kind == "uptodate"


def test_interpret_blocks_unsafe_download_url():
    d = _uc()._interpret({"latest_version": "2.0",
                          "download_url": "javascript:alert(1)"})
    assert d.download_url is None      # is_safe_http_url 로 차단


# ------------------- 저장 단계 실패 판별 / 오류 문구 -------------------
# 배경: 4K 다운로드가 '받기'는 다 끝내고 마지막 이름 바꾸기에서 WinError 5 로
# 죽었는데, 앱은 재시도 사다리 맨 끝(best/worst)의 엉뚱한 영어 메시지를 띄웠다.

def test_looks_like_output_error():
    assert main.looks_like_output_error(
        "[WinError 5] 액세스가 거부되었습니다: 'a.temp.mp4' -> 'a.mp4'")
    assert main.looks_like_output_error("PermissionError: Permission denied")
    assert main.looks_like_output_error(
        "[WinError 32] 다른 프로세스가 사용 중이기 때문에")
    assert not main.looks_like_output_error(
        "ERROR: [youtube] x: Requested format is not available")


def test_friendly_error_explains_real_cause():
    out = main.friendly_error("[WinError 5] 액세스가 거부되었습니다: 'a' -> 'b'")
    assert "저장 폴더" in out and "WinError" not in out
    assert "화질" in main.friendly_error(
        "ERROR: [youtube] x: Requested format is not available.")
    assert "쿠키" in main.friendly_error(
        "ERROR: Could not copy Chrome cookie database. See ...")
    assert main.friendly_error("") == "다운로드에 실패했습니다."
    # 모르는 오류는 첫 줄만 남긴다 (마지막 줄은 잘린 경로 조각일 때가 많다)
    assert main.friendly_error("진짜 원인\n  -> 경로 조각") == "진짜 원인"


# --------------- 작업 폴더 → 저장 폴더 옮기기 (_harvest) ---------------

def test_free_path_uniquifies(tmp_path):
    assert Downloader._free_path(str(tmp_path), "a.mp4") == tmp_path / "a.mp4"
    (tmp_path / "a.mp4").write_bytes(b"x")
    assert Downloader._free_path(str(tmp_path), "a.mp4") == tmp_path / "a (2).mp4"
    (tmp_path / "a (2).mp4").write_bytes(b"x")
    assert Downloader._free_path(str(tmp_path), "a.mp4") == tmp_path / "a (3).mp4"


def test_harvest_moves_only_finished_files(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    work.mkdir(), out.mkdir()
    (work / "영상.mp4").write_bytes(b"video")
    (work / "영상.f137.mp4.part").write_bytes(b"partial")   # 받다 만 조각
    (work / "영상.webp").write_bytes(b"thumb")              # 심고 남은 썸네일

    seen: list[str] = []
    dl = Downloader(on_progress=lambda d: None, on_done=lambda ok, e: None,
                    on_item_done=lambda vid, fp=None: seen.append(fp))
    dl._tmpdir = str(work)
    dl._harvest(str(out))

    assert (out / "영상.mp4").read_bytes() == b"video"
    assert sorted(p.name for p in out.iterdir()) == ["영상.mp4"]
    assert seen == [str(out / "영상.mp4")]     # 진짜 경로로 UI 에 정정 통보


def test_harvest_skips_half_merged_file(tmp_path):
    """'제목.temp.mp4' 는 합치다 만 깨진 파일이라 내보내면 안 된다."""
    work, out = tmp_path / "work", tmp_path / "out"
    work.mkdir(), out.mkdir()
    (work / "영상.temp.mp4").write_bytes(b"half")
    (work / "내.temp.영상.mp4").write_bytes(b"ok")   # 이름에 .temp 가 낀 정상 파일

    dl = _dl()
    dl._tmpdir = str(work)
    dl._harvest(str(out))

    assert not (out / "영상.temp.mp4").exists()
    assert (out / "내.temp.영상.mp4").read_bytes() == b"ok"


def test_harvest_never_overwrites_existing(tmp_path):
    """같은 영상을 두 번 받아도 기존 파일을 건드리지 않는다.
    예전엔 여기서 덮어쓰기를 시도하다 '액세스가 거부' 로 다운로드가 죽었다."""
    work, out = tmp_path / "work", tmp_path / "out"
    work.mkdir(), out.mkdir()
    (out / "영상.mp4").write_bytes(b"old")
    (work / "영상.mp4").write_bytes(b"new")

    dl = _dl()
    dl._tmpdir = str(work)
    dl._harvest(str(out))

    assert (out / "영상.mp4").read_bytes() == b"old"        # 기존 파일 그대로
    assert (out / "영상 (2).mp4").read_bytes() == b"new"


def test_base_opts_works_inside_workdir(tmp_path):
    """yt-dlp 는 작업 폴더 안에서만 일한다 → 저장 폴더 충돌 자체가 없다."""
    dl = _dl()
    dl._tmpdir = str(tmp_path / "work")
    opts = dl._base_opts(_cfg(out_dir=str(tmp_path / "out")))
    assert opts["paths"] == {"home": dl._tmpdir, "temp": dl._tmpdir}


def test_base_opts_falls_back_to_out_dir_without_workdir(tmp_path):
    dl = _dl()
    dl._tmpdir = None
    out = str(tmp_path / "out")
    assert dl._base_opts(_cfg(out_dir=out))["paths"] == {"home": out}


# ------------------- 재시도 사다리의 오류 보고 -------------------

class _FakeYDL:
    """download() 가 정해진 예외를 던지는 yt_dlp.YoutubeDL 대역."""
    raises: Exception = RuntimeError("boom")
    calls: list[int] = []

    def __init__(self, opts): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def download(self, urls):
        type(self).calls.append(1)
        raise type(self).raises


def _run_with_fake_ydl(monkeypatch, tmp_path, exc):
    _FakeYDL.calls = []
    _FakeYDL.raises = exc
    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", _FakeYDL)
    monkeypatch.setattr(main.Downloader, "_try_stage2", lambda self, c, b: False)
    monkeypatch.setattr(main, "detect_cookie_browser", lambda: None)
    done: list[tuple] = []
    dl = Downloader(on_progress=lambda d: None,
                    on_done=lambda ok, e: done.append((ok, e)))
    dl.run(_cfg(url="https://s.com/v", out_dir=str(tmp_path)))
    return done[-1], len(_FakeYDL.calls)


def test_reports_first_error_not_last(monkeypatch, tmp_path):
    """사다리 맨 끝은 'best/worst 로 낮춰보기'라 유튜브에선 늘 같은 말로
    끝난다. 그 메시지가 첫 시도의 진짜 원인을 덮으면 안 된다."""
    (ok, err), tries = _run_with_fake_ydl(
        monkeypatch, tmp_path, RuntimeError("ERROR: 첫 시도가 알려준 진짜 원인"))
    assert ok is False
    assert "진짜 원인" in err
    assert tries > 1                     # 사다리는 정상적으로 다 타봤다


def test_stops_retrying_on_output_error(monkeypatch, tmp_path):
    """저장 실패는 방식을 바꿔도 똑같이 실패한다 → 즉시 멈추고 원인을 알린다."""
    (ok, err), tries = _run_with_fake_ydl(
        monkeypatch, tmp_path,
        OSError("[WinError 5] 액세스가 거부되었습니다: 'a.temp.mp4' -> 'a.mp4'"))
    assert ok is False
    assert tries == 1                    # 사다리를 타지 않는다
    assert "저장 폴더" in err


# ------------------- 앱 자동 업데이트 (AppUpdater) -------------------

def _api_resp(payload: dict, monkeypatch):
    raw = json.dumps(payload).encode("utf-8")

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return raw

    monkeypatch.setattr(main.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp())


def test_app_updater_picks_exe_asset(monkeypatch):
    _api_resp({"assets": [
        {"name": "notes.txt", "browser_download_url": "https://x/notes.txt"},
        {"name": "Setup-v1.9.exe", "size": 123, "digest": "sha256:ABC123",
         "browser_download_url": "https://github.com/o/r/d/Setup-v1.9.exe"},
    ]}, monkeypatch)
    a = main.AppUpdater()._from_api("1.9")
    assert a.url.endswith("Setup-v1.9.exe")
    assert a.size == 123
    assert a.sha256 == "abc123"            # 소문자 hex


def test_app_updater_ignores_non_https_asset(monkeypatch):
    _api_resp({"assets": [
        {"name": "Setup.exe", "browser_download_url": "http://x/Setup.exe"},
    ]}, monkeypatch)
    with pytest.raises(RuntimeError):      # 실행 파일을 평문 http 로 받지 않는다
        main.AppUpdater()._from_api("1.9")


def test_app_updater_errors_when_release_has_no_exe(monkeypatch):
    _api_resp({"assets": [{"name": "notes.txt",
                           "browser_download_url": "https://x/notes.txt"}]},
              monkeypatch)
    with pytest.raises(RuntimeError):
        main.AppUpdater()._from_api("1.9")


def test_app_updater_resolve_prefers_explicit_url(monkeypatch):
    """version.json 이 직접 지정하면 API 를 부르지 않는다 (비상 탈출구)."""
    def boom(*a, **k):
        raise AssertionError("API 를 부르면 안 된다")
    monkeypatch.setattr(main.urllib.request, "urlopen", boom)
    d = main.UpdateDecision(kind="new_version", latest_version="1.9",
                            installer_url="https://x/s.exe",
                            installer_sha256="ab", installer_size=7)
    a = main.AppUpdater().resolve(d)
    assert (a.url, a.sha256, a.size) == ("https://x/s.exe", "ab", 7)


def _asset(**kw):
    return main.InstallerAsset(url=kw.pop("url", "https://x/s.exe"), **kw)


def test_app_updater_verify_accepts_good_exe(tmp_path, monkeypatch):
    monkeypatch.setattr(main.AppUpdater, "MIN_EXE_BYTES", 0)
    exe = tmp_path / "s.exe"
    exe.write_bytes(b"MZ" + b"\0" * 100)
    main.AppUpdater._verify(exe, _asset(size=102,
                                        sha256=main.AppUpdater._sha256(exe)))


@pytest.mark.parametrize("bad", [
    dict(size=999),                 # 릴리스가 알려준 크기와 다름 (전송 중 손상)
    dict(sha256="deadbeef"),        # 해시 불일치 (변조 의심)
])
def test_app_updater_verify_rejects_mismatch(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(main.AppUpdater, "MIN_EXE_BYTES", 0)
    exe = tmp_path / "s.exe"
    exe.write_bytes(b"MZ" + b"\0" * 100)
    with pytest.raises(RuntimeError):
        main.AppUpdater._verify(exe, _asset(**bad))


def test_app_updater_verify_rejects_non_executable(tmp_path, monkeypatch):
    """오류 페이지(HTML)를 받아놓고 설치본이라 믿는 일이 없도록."""
    monkeypatch.setattr(main.AppUpdater, "MIN_EXE_BYTES", 0)
    page = tmp_path / "s.exe"
    page.write_bytes(b"<html>Not Found</html>")
    with pytest.raises(RuntimeError):
        main.AppUpdater._verify(page, _asset())


def test_app_updater_verify_rejects_absurd_size(tmp_path):
    tiny = tmp_path / "s.exe"
    tiny.write_bytes(b"MZ")
    with pytest.raises(RuntimeError):      # 5MB 미만 → 잘린 파일
        main.AppUpdater._verify(tiny, _asset())


def test_app_updater_refuses_plain_http_download():
    with pytest.raises(RuntimeError):
        main.AppUpdater().download(_asset(url="http://x/s.exe"), lambda p: None)


def test_setup_args_match_installer_script():
    """installer.iss 의 WantsAutoRun 이 읽는 인자와 어긋나면 설치 후 앱이
    다시 안 켜진다. 이름이 바뀌면 여기서 먼저 걸리게 둔다."""
    assert "/AUTORUN=1" in main.AppUpdater.SETUP_ARGS
    assert "/SILENT" in main.AppUpdater.SETUP_ARGS
    iss = (main.Path(__file__).parent / "installer.iss").read_text(encoding="utf-8-sig")
    assert "{param:AUTORUN|0}" in iss


# ------------- version.json → 설치본 정보 전달 -------------

def test_interpret_carries_installer_override():
    d = _uc()._interpret({"latest_version": "2.0",
                          "installer_url": "https://x/s.exe",
                          "installer_sha256": "abc", "installer_size": 12})
    assert d.installer_url == "https://x/s.exe"
    assert d.installer_sha256 == "abc" and d.installer_size == 12


def test_interpret_blocks_unsafe_installer_url():
    d = _uc()._interpret({"latest_version": "2.0",
                          "installer_url": "file:///C:/evil.exe"})
    assert d.installer_url is None      # API 조회로 폴백


def test_interpret_defaults_to_api_lookup():
    """평소 version.json 에는 설치본 정보를 안 적는다 → AppUpdater 가 찾는다."""
    d = _uc()._interpret({"latest_version": "2.0"})
    assert d.installer_url is None and d.installer_size == 0


def test_interpret_force_update_can_self_install():
    """강제 업데이트도 앱 안에서 받아 설치한다 → 버전을 들고 있어야 한다."""
    d = _uc()._interpret({"min_supported_version": "1.5", "latest_version": "2.0"})
    assert d.kind == "force_update" and d.latest_version == "2.0"


# ------------------- 작업 폴더 위치 / 뒷정리 -------------------

def test_workdir_defaults_to_app_folder(monkeypatch, tmp_path):
    """저장 폴더와 같은 드라이브면 앱 폴더에 만든다 (저장 폴더는 안 건드림)."""
    app_root = tmp_path / "app"
    monkeypatch.setattr(main, "download_temp_root", lambda: app_root)
    out = tmp_path / "out"
    work = _dl()._make_workdir(str(out))
    assert work is not None
    assert main.Path(work).parent == app_root
    assert not out.exists()


def test_workdir_follows_save_dir_on_other_volume(monkeypatch, tmp_path):
    """다른 드라이브면 저장 폴더 안에 만든다 — 안 그러면 마지막 '옮기기'가
    통째 복사가 돼서 4K·8K 에서 눈에 띄게 느려진다."""
    monkeypatch.setattr(main, "download_temp_root", lambda: tmp_path / "app")
    monkeypatch.setattr(main.Downloader, "_same_volume",
                        staticmethod(lambda a, b: False))
    out = tmp_path / "out"
    work = main.Path(_dl()._make_workdir(str(out)))
    assert work.parent == out / main.WORKDIR_NAME


def test_run_cleans_up_workdir_in_save_dir(monkeypatch, tmp_path):
    """작업 폴더 껍데기가 저장 폴더에 남으면 안 된다 (v1.8 에서 고친 문제)."""
    monkeypatch.setattr(main, "download_temp_root", lambda: tmp_path / "app")
    monkeypatch.setattr(main.Downloader, "_same_volume",
                        staticmethod(lambda a, b: False))
    out = tmp_path / "out"
    (ok, _err), _ = _run_with_fake_ydl_in(monkeypatch, out, RuntimeError("nope"))
    assert ok is False
    assert not (out / main.WORKDIR_NAME).exists()


def _run_with_fake_ydl_in(monkeypatch, out_dir, exc):
    _FakeYDL.calls = []
    _FakeYDL.raises = exc
    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", _FakeYDL)
    monkeypatch.setattr(main.Downloader, "_try_stage2", lambda self, c, b: False)
    monkeypatch.setattr(main, "detect_cookie_browser", lambda: None)
    done: list[tuple] = []
    dl = Downloader(on_progress=lambda d: None,
                    on_done=lambda ok, e: done.append((ok, e)))
    dl.run(_cfg(url="https://s.com/v", out_dir=str(out_dir)))
    return done[-1], len(_FakeYDL.calls)
