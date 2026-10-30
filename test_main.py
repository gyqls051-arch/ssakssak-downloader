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
    build_format_selector, build_format_sort, compare_versions,
    is_safe_http_url, parse_version,
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
    sel = build_format_selector("mp4")
    # 해상도·비디오 코덱은 하드 필터링하지 않고 format_sort가 맡는다 —
    # vcodec^=avc1 필터는 4K(VP9/AV1)와 고화질 쇼츠·릴스를 1080p·360p로 제한했다.
    assert "height<=" not in sel and "vcodec^=" not in sel and "mp4a" in sel


def test_build_format_selector_mkv_best():
    assert build_format_selector("mkv") == "bestvideo+bestaudio/best"


def test_build_format_sort_uses_short_edge():
    # res=min(width,height)라 세로 1080×1920도 1080p로 인식된다.
    assert build_format_sort(1080) == ["res:1080"]
    assert build_format_sort(None) == []
    assert build_format_sort(-1) == []
    assert build_format_sort(None, "mp4") == ["res", "vcodec:h264", "acodec:aac"]
    assert build_format_sort(1080, "mp4") == ["res:1080", "vcodec:h264", "acodec:aac"]
    assert build_format_sort(-1, "mp4") == []


def test_mp4_format_selection_picks_4k_and_hd_shorts_over_low_res_avc():
    sel = build_format_selector("mp4")
    formats_4k = [
        {"format_id": "137", "url": "https://example.com/137", "ext": "mp4",
         "width": 1920, "height": 1080, "vcodec": "avc1.640028", "acodec": "none", "tbr": 3500},
        {"format_id": "248", "url": "https://example.com/248", "ext": "webm",
         "width": 1920, "height": 1080, "vcodec": "vp9", "acodec": "none", "tbr": 4500},
        {"format_id": "313", "url": "https://example.com/313", "ext": "webm",
         "width": 3840, "height": 2160, "vcodec": "vp9", "acodec": "none", "tbr": 18000},
        {"format_id": "140", "url": "https://example.com/140", "ext": "m4a",
         "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128},
    ]
    with main.yt_dlp.YoutubeDL({
        "format": sel, "format_sort": build_format_sort(None, "mp4"), "quiet": True,
    }) as ydl:
        res_auto = ydl.process_ie_result({
            "id": "t1", "title": "t1", "extractor": "t", "extractor_key": "T",
            "formats": [dict(f) for f in formats_4k],
        }, download=False)
    assert res_auto["requested_formats"][0]["height"] == 2160

    # 동일 해상도(1080p)에서는 호환성 좋은 H.264(137)가 더 높은 비트레이트 VP9(248)보다 우선
    with main.yt_dlp.YoutubeDL({
        "format": sel, "format_sort": build_format_sort(1080, "mp4"), "quiet": True,
    }) as ydl:
        res_1080 = ydl.process_ie_result({
            "id": "t2", "title": "t2", "extractor": "t", "extractor_key": "T",
            "formats": [dict(f) for f in formats_4k],
        }, download=False)
    assert res_1080["requested_formats"][0]["format_id"] == "137"

    # 세로 영상에서 H.264가 360×640뿐이고 1080×1920이 VP9일 때 360p로 떨어지지 않는다
    shorts = [
        {"format_id": "18", "url": "https://example.com/18", "ext": "mp4",
         "width": 360, "height": 640, "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "tbr": 500},
        {"format_id": "248", "url": "https://example.com/248", "ext": "webm",
         "width": 1080, "height": 1920, "vcodec": "vp9", "acodec": "none", "tbr": 3500},
        {"format_id": "140", "url": "https://example.com/140", "ext": "m4a",
         "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128},
    ]
    with main.yt_dlp.YoutubeDL({
        "format": sel, "format_sort": build_format_sort(None, "mp4"), "quiet": True,
    }) as ydl:
        res_shorts = ydl.process_ie_result({
            "id": "t3", "title": "t3", "extractor": "t", "extractor_key": "T",
            "formats": shorts,
        }, download=False)
    assert (res_shorts["requested_formats"][0]["width"],
            res_shorts["requested_formats"][0]["height"]) == (1080, 1920)


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

def test_cancel_raises_in_progress_hook(tmp_path, monkeypatch):
    seen = []
    dl = Downloader(on_progress=lambda d: seen.append(d),
                    on_done=lambda ok, e: None)
    dl._progress_hook({"status": "downloading"})   # 취소 전: 위임
    assert seen == [{"status": "downloading"}]
    dl.cancel()
    with pytest.raises(main.yt_dlp.utils.DownloadCancelled):
        dl._progress_hook({"status": "downloading"})

    # publish 직후 cancel이 먼저 terminal state를 차지하면 최종 파일은 rollback되고
    # 작업 폴더 원본과 복구 가능성은 그대로 남아야 한다.
    work, out = tmp_path / "work", tmp_path / "out"
    work.mkdir(), out.mkdir()
    source = work / "영상.mp4"
    source.write_bytes(b"complete")
    completed = []
    racing = Downloader(
        on_progress=lambda d: None, on_done=lambda ok, e: None,
        on_item_done=lambda vid, path, dims=None: completed.append(path),
    )
    racing._tmpdir = str(work)
    publish = racing._publish_completed_file

    def publish_then_cancel(src, out_dir):
        destination = publish(src, out_dir)
        racing.cancel()
        return destination

    monkeypatch.setattr(racing, "_publish_completed_file", publish_then_cancel)
    with pytest.raises(main.yt_dlp.utils.DownloadCancelled):
        racing._harvest(str(out))
    assert source.read_bytes() == b"complete"
    assert list(out.iterdir()) == []
    assert completed == []


def test_postprocessor_hook_forwards_final_path():
    calls = []
    dl = Downloader(on_progress=lambda d: None, on_done=lambda ok, e: None,
                    on_item_done=lambda vid, fp, dims=None:
                    calls.append((vid, fp)))
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


def test_settings_save_is_atomic_no_partial(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    s = Settings(p)
    assert s.save({"a": 1, "cookie_opt_in": False}) is True
    assert json.loads(p.read_text(encoding="utf-8")) == {
        "a": 1, "cookie_opt_in": False}

    # atomic replace 실패를 주입해도 디스크·메모리 설정은 모두 이전 상태여야 하고
    # mkstemp의 unique 임시 파일도 남지 않아야 한다.
    def fail_replace(src, dst):
        raise OSError("injected replace failure")

    monkeypatch.setattr(main.os, "replace", fail_replace)
    api = main.Api.__new__(main.Api)
    api._state_lock = main.threading.RLock()
    api._settings = s
    api._cfg = {"a": 1, "cookie_opt_in": False}
    api._analysis_contexts = {}
    result = api.set_cookie_opt_in(True)

    assert result == {"ok": False, "error": "설정을 저장하지 못했습니다",
                      "enabled": False}
    assert api._cfg == {"a": 1, "cookie_opt_in": False}
    assert json.loads(p.read_text(encoding="utf-8")) == api._cfg
    assert list(tmp_path.glob("config.json.*.tmp")) == []


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
    monkeypatch.setattr(FfmpegInstaller, "MIN_EXE_BYTES", 0)
    content = b"MZdata"
    good = tmp_path / "good.zip"
    _make_zip(good, "ffmpeg-x/bin/ffmpeg.exe", content)
    digest = FfmpegInstaller._sha256(good)
    FfmpegInstaller._verify_zip(
        good, digest, good.stat().st_size, len(content))

    with pytest.raises(RuntimeError):
        FfmpegInstaller._verify_zip(good, "deadbeef")
    with pytest.raises(RuntimeError):
        FfmpegInstaller._verify_zip(
            good, digest, good.stat().st_size, len(content) + 1)


def test_verify_zip_rejects_wrong_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(FfmpegInstaller, "MIN_ZIP_BYTES", 0)
    monkeypatch.setattr(FfmpegInstaller, "MAX_ZIP_BYTES", 10**9)
    bad = tmp_path / "bad.zip"
    _make_zip(bad, "some/other/file.txt")
    with pytest.raises(RuntimeError):           # ffmpeg.exe 없음
        FfmpegInstaller._verify_zip(bad)


def test_ffmpeg_replace_with_retry_handles_transient_av_lock(tmp_path, monkeypatch):
    src = tmp_path / "ffmpeg.part.exe"
    dst = tmp_path / "ffmpeg.exe"
    src.write_bytes(b"MZok")
    real_replace = main.os.replace
    attempts = 0

    def flaky_replace(a, b):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(32, "being used by another process")
        return real_replace(a, b)

    monkeypatch.setattr(main.os, "replace", flaky_replace)
    FfmpegInstaller._replace_with_retry(src, dst, attempts=5, delay=0.0)
    assert attempts == 3
    assert dst.read_bytes() == b"MZok"


def test_ffmpeg_static_pins_resolve_without_api(monkeypatch):
    tag = "autobuild-2026-08-30-12-34"
    name = "ffmpeg-pinned-win64-gpl.zip"
    url = f"https://github.com/BtbN/FFmpeg-Builds/releases/download/{tag}/{name}"
    monkeypatch.setattr(FfmpegInstaller, "RELEASE_TAG", tag)
    monkeypatch.setattr(FfmpegInstaller, "ASSET_NAME", name)
    monkeypatch.setattr(FfmpegInstaller, "URL", url)
    monkeypatch.setattr(FfmpegInstaller, "EXPECTED_SIZE",
                        FfmpegInstaller.MIN_ZIP_BYTES)
    monkeypatch.setattr(FfmpegInstaller, "EXPECTED_SHA256", "a" * 64)
    monkeypatch.setattr(FfmpegInstaller, "EXPECTED_EXE_SIZE",
                        FfmpegInstaller.MIN_EXE_BYTES)
    monkeypatch.setattr(FfmpegInstaller, "EXPECTED_EXE_SHA256", "b" * 64)

    def network_forbidden(*args, **kwargs):
        raise AssertionError("static ffmpeg pin resolution must not use network metadata")

    monkeypatch.setattr(main.urllib.request, "urlopen", network_forbidden)
    asset = FfmpegInstaller._resolve_asset()
    assert (asset.url, asset.size, asset.sha256) == (
        url, FfmpegInstaller.MIN_ZIP_BYTES, "a" * 64)
    assert (asset.executable_size, asset.executable_sha256) == (
        FfmpegInstaller.MIN_EXE_BYTES, "b" * 64)


@pytest.mark.parametrize("field,empty", [("RELEASE_TAG", ""), ("ASSET_NAME", ""), ("URL", ""), ("EXPECTED_SIZE", 0), ("EXPECTED_SHA256", ""), ("EXPECTED_EXE_SIZE", 0), ("EXPECTED_EXE_SHA256", "")])
def test_ffmpeg_missing_static_pins_fail_before_network(monkeypatch, field, empty):
    monkeypatch.setattr(main.FfmpegInstaller, field, empty)
    called = False

    def unexpected_network(*args, **kwargs):
        nonlocal called
        called = True
        raise OSError("network must not be reached")

    monkeypatch.setattr(main.urllib.request, "urlopen", unexpected_network)
    with pytest.raises(RuntimeError):
        FfmpegInstaller._resolve_asset()
    assert called is False


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


def test_attempt_plan_adds_cookie_step_when_explicitly_allowed():
    plan = _dl()._attempt_plan(_cfg(cookies_browser="chrome"))
    cookie_extra = next(extra for _label, extra in plan
                        if "cookiesfrombrowser" in extra)
    assert cookie_extra["cookiesfrombrowser"] == ("chrome", None, None, None)


def test_base_opts_has_socket_timeout_and_hooks():
    dl = _dl()
    dl._tmpdir = "/tmp/ssak-work"
    opts = dl._base_opts(_cfg())
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

    monkeypatch.setattr(main, "validate_public_http_url",
                        lambda url, **kwargs: url)
    monkeypatch.setattr(main, "_open_public_url",
                        lambda req, timeout=0: _Resp())
    got = Downloader._scan_manifest("https://c.com/watch")
    assert got == "https://c.com/v/master.m3u8?a=1"     # .m3u8 우선 + \/ 복원


# ------------------- UpdateChecker 판정 -------------------

def _uc():
    return main.UpdateChecker(current_version="1.0")


def test_interpret_new_version_carries_notes_and_url():
    d = _uc()._interpret({"latest_version": "2.0",
                          "release_notes": "메모",
                          "download_url": "https://attacker.example/dl"})
    assert d.kind == "new_version"
    assert d.latest_version == "2.0" and d.release_notes == "메모"
    assert d.download_url == (
        f"https://github.com/{main.GITHUB_RELEASE_REPO}/releases/tag/v2.0")
    assert "attacker.example" not in d.download_url


def test_interpret_kinds():
    # unsigned manifest의 killswitch/min_supported_version은 권한이 없다.
    assert _uc()._interpret({"killswitch": True}).kind == "uptodate"
    assert _uc()._interpret({"min_supported_version": "1.5"}).kind == "uptodate"
    assert _uc()._interpret({"latest_version": "1.0"}).kind == "uptodate"


def test_interpret_blocks_unsafe_download_url():
    d = _uc()._interpret({"latest_version": "2.0",
                          "download_url": "javascript:alert(1)"})
    assert d.download_url == (
        f"https://github.com/{main.GITHUB_RELEASE_REPO}/releases/tag/v2.0")
    assert "javascript:" not in d.download_url


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
                    on_item_done=lambda vid, fp=None, dims=None:
                    seen.append(fp))
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


def test_base_opts_fails_closed_without_workdir(tmp_path, monkeypatch):
    dl = _dl()
    dl._tmpdir = None
    with pytest.raises(RuntimeError):
        dl._base_opts(_cfg(out_dir=str(tmp_path / "out")))

    done = []
    guarded = Downloader(on_progress=lambda d: None,
                         on_done=lambda ok, e: done.append((ok, e)))
    monkeypatch.setattr(guarded, "_make_workdir", lambda out_dir: None)
    guarded.run(_cfg(out_dir=str(tmp_path / "out")))
    assert done and done[-1][0] is False
    assert "작업 폴더" in done[-1][1]
    assert not (tmp_path / "out").exists()


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
    name = main.CANONICAL_SETUP_NAME_FMT.format(version="1.9")
    digest = "a" * 64
    size = 6 * 1024 * 1024
    _api_resp({"assets": [
        {"name": "notes.txt", "browser_download_url": "https://x/notes.txt"},
        {"name": name, "size": size, "digest": "sha256:" + digest,
         "browser_download_url": "https://github.com/o/r/releases/download/v1.9/" + name},
    ]}, monkeypatch)
    a = main.AppUpdater()._from_api("1.9")
    assert a.url.endswith(name)
    assert a.size == size
    assert a.sha256 == digest


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


def test_app_updater_resolve_ignores_manifest_override(monkeypatch):
    """decision에 override 속성이 주입돼도 공식 release API만 사용한다."""
    name = main.CANONICAL_SETUP_NAME_FMT.format(version="1.9")
    digest = "b" * 64
    size = 6 * 1024 * 1024
    official = (
        "https://github.com/gyqls051-arch/ssakssak-downloader/"
        f"releases/download/v1.9/{name}"
    )
    _api_resp({"assets": [{
        "name": name, "size": size, "digest": "sha256:" + digest,
        "browser_download_url": official,
    }]}, monkeypatch)
    decision = main.UpdateDecision(kind="new_version", latest_version="1.9")
    decision.installer_url = "https://attacker.example/evil.exe"
    decision.installer_sha256 = "c" * 64
    decision.installer_size = size

    asset = main.AppUpdater().resolve(decision)
    assert (asset.url, asset.sha256, asset.size) == (official, digest, size)


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
    """설치 재실행 인자와 release toolchain의 exact 실행 경로 계약을 함께 검증."""
    assert "/AUTORUN=1" in main.AppUpdater.SETUP_ARGS
    assert "/SILENT" in main.AppUpdater.SETUP_ARGS
    root = main.Path(__file__).parent
    iss = (root / "installer.iss").read_text(encoding="utf-8-sig")
    assert "{param:AUTORUN|0}" in iss
    assert 'Source: "engine\\*"' not in iss

    lock = json.loads((root / "bundle-release-lock.json").read_text(encoding="utf-8"))
    bundle = (root / "prepare_bundle.py").read_text(encoding="utf-8")
    assert lock["npm_version"] == "10.9.3"
    assert f'NPM_VERSION = "{lock["npm_version"]}"' in bundle
    assert 'node, str(npm_cli), "ci"' in bundle
    assert 'node, str(tsc)' in bundle
    assert 'node, str(npm_cli), "prune"' in bundle
    assert 'run(["npm"' not in bundle and "def build_engine" not in bundle
    assert "NPM_CONFIG_USERCONFIG" in bundle and "NPM_CONFIG_CACHE" in bundle


# ------------- unsigned version.json 권한 축소 -------------

def test_interpret_carries_installer_override():
    d = _uc()._interpret({"latest_version": "2.0",
                          "installer_url": "https://attacker.example/s.exe",
                          "installer_sha256": "abc", "installer_size": 12})
    assert not hasattr(d, "installer_url")
    assert d.download_url.endswith("/releases/tag/v2.0")
    assert "attacker.example" not in d.download_url


def test_interpret_blocks_unsafe_installer_url():
    d = _uc()._interpret({"latest_version": "2.0",
                          "installer_url": "file:///C:/evil.exe"})
    assert not hasattr(d, "installer_url")
    assert d.kind == "new_version"


def test_interpret_defaults_to_api_lookup():
    """실행 asset은 manifest가 아니라 AppUpdater의 local canonical API 계약이 찾는다."""
    d = _uc()._interpret({"latest_version": "2.0"})
    assert not hasattr(d, "installer_size")
    assert d.download_url == (
        f"https://github.com/{main.GITHUB_RELEASE_REPO}/releases/tag/v2.0")


def test_interpret_force_update_can_self_install():
    """unsigned min_supported_version은 강제 상태를 만들지 못한다."""
    current = _uc()._interpret({"min_supported_version": "9.9"})
    newer = _uc()._interpret({"min_supported_version": "9.9",
                              "latest_version": "2.0"})
    assert current.kind == "uptodate"
    assert newer.kind == "new_version" and newer.latest_version == "2.0"


# ------------------- 작업 폴더 위치 / 뒷정리 -------------------

def test_workdir_defaults_to_app_folder(monkeypatch, tmp_path):
    """저장 폴더와 같은 드라이브면 앱 폴더에 만든다 (저장 폴더는 안 건드림)."""
    app_root = tmp_path / "app"
    monkeypatch.setattr(main, "download_temp_root", lambda: app_root)
    # _same_volume은 Windows 드라이브 문자 기반이라 POSIX에선 항상 False —
    # 이 테스트는 '같은 드라이브' 시나리오 자체가 목적이므로 스텁한다.
    monkeypatch.setattr(main.Downloader, "_same_volume",
                        staticmethod(lambda a, b: True))
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


def test_app_updater_uses_absolute_system_powershell(monkeypatch, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace
    monkeypatch.setattr(main, "TRUSTED_SIGNER_THUMBPRINTS", ("A" * 40,))
    def run(args, **kwargs):
        assert Path(args[0]).is_absolute(), "Never resolve the trust verifier through PATH"
        assert Path(args[0]).name.lower() == "powershell.exe"
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "Status": "Valid", "Thumbprint": "A" * 40, "ProductVersion": "1.10"}))
    monkeypatch.setattr(main.subprocess, "run", run)
    main.AppUpdater._verify_windows_trust(tmp_path / "Setup.exe", "1.10")


# -------- v1.11.4: 썸네일 / MP4 호환 변환 / 탐색기 선택 / 대기열 --------

def test_video_analyzer_thumb_fetches_validated_data_uri(monkeypatch):
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"

    class _ThumbResp:
        def __init__(self, payload: bytes):
            self._payload = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return self._payload[:n] if n >= 0 else self._payload

    monkeypatch.setattr(main, "validate_public_http_url", lambda url, **kw: url)
    monkeypatch.setattr(main, "_open_public_url",
                        lambda req, timeout=0: _ThumbResp(jpeg_bytes))
    uri = main.VideoAnalyzer._thumb({
        "thumbnails": [
            {"url": "http://insecure.example/t.jpg", "width": 480},
            {"url": "https://cdn.example/t480.jpg", "width": 480},
        ],
    })
    assert uri.startswith("data:image/jpeg;base64,")

    # HTML/비이미지 응답은 거부한다.
    monkeypatch.setattr(main, "_open_public_url",
                        lambda req, timeout=0: _ThumbResp(b"<html>nope</html>"))
    assert main.VideoAnalyzer._thumb({"thumbnail": "https://cdn.example/bad.jpg"}) == ""


def test_ensure_mp4_compatible_converts_vp9_and_skips_clean_h264(monkeypatch, tmp_path):
    ffmpeg_dir = tmp_path / "bin"
    ffmpeg_dir.mkdir()
    ffmpeg_name = "ffmpeg.exe" if main.sys.platform == "win32" else "ffmpeg"
    (ffmpeg_dir / ffmpeg_name).write_bytes(b"MZ")

    work = tmp_path / "work"
    work.mkdir()
    clip = work / "reel.mp4"
    clip.write_bytes(b"vp9-raw")

    dl = _dl()
    dl._tmpdir = str(work)
    cfg = DownloadConfig(
        url="https://s.com/v", out_dir=str(tmp_path / "out"),
        format_str="FMT", is_audio_only=False,
        container="mp4", ffmpeg_dir=str(ffmpeg_dir),
    )

    # 1) 이미 깨끗한 H.264 + AAC-LC 면 재인코딩하지 않는다.
    monkeypatch.setattr(
        Downloader, "_inspect_mp4_streams",
        staticmethod(lambda exe, m: ("h264", "aac", "Stream #0:1: Audio: aac (LC)", False)),
    )
    popen_calls: list[list[str]] = []
    monkeypatch.setattr(main.subprocess, "Popen",
                        lambda cmd, **kw: popen_calls.append(cmd))
    dl._ensure_mp4_compatible(cfg)
    assert popen_calls == []
    assert clip.read_bytes() == b"vp9-raw"

    # 2) VP9 / HE-AAC 스트림이면 libx264 + aac + +faststart 변환으로 교체한다.
    monkeypatch.setattr(
        Downloader, "_inspect_mp4_streams",
        staticmethod(lambda exe, m: ("vp9", "aac", "Stream #0:1: Audio: aac (HE-AAC)", True)),
    )

    class _FakeProc:
        def __init__(self, cmd):
            popen_calls.append(cmd)
            main.Path(cmd[-1]).write_bytes(b"h264-converted")
            self.returncode = 0
            self.stderr = None
        def poll(self): return 0

    monkeypatch.setattr(main.subprocess, "Popen", lambda cmd, **kw: _FakeProc(cmd))
    dl._ensure_mp4_compatible(cfg)
    assert len(popen_calls) == 1
    assert "libx264" in popen_calls[0] and "+faststart" in popen_calls[0]
    assert clip.read_bytes() == b"h264-converted"


def test_open_item_falls_back_to_parent_folder_when_reveal_fails(monkeypatch, tmp_path):
    media = tmp_path / "싹싹김치 다운로드" / "Video by orion_world.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"mp4")

    api = main.Api.__new__(main.Api)
    api._state_lock = main.threading.RLock()
    job_id = "d" + "a" * 32
    api._files = {job_id: str(media)}
    api._job_roots = {job_id: str(media.parent)}

    opened: list[str] = []
    monkeypatch.setattr(main, "_reveal_in_explorer", lambda p: False)
    monkeypatch.setattr(main.os, "startfile", lambda p: opened.append(p), raising=False)
    api.open_item(job_id)
    assert opened == [str(media.parent.resolve())]


def test_start_download_queues_beyond_max_active_and_drains(monkeypatch, tmp_path):
    import threading as _th

    monkeypatch.setattr(main, "validate_public_http_url", lambda url, **kw: url)
    gate = _th.Event()
    ran: list[str] = []

    def fake_run(self, cfg):
        gate.wait(timeout=3.0)
        ran.append(cfg.url)

    monkeypatch.setattr(Downloader, "run", fake_run)

    api = main.Api.__new__(main.Api)
    api._window = None
    api._closing = False
    api._updating = False
    api._state_lock = _th.RLock()
    api._settings = Settings(tmp_path / "cfg.json")
    api._cfg = {"save_dir": str(tmp_path / "out"), "cookie_opt_in": False}
    api._ffmpeg_dir = str(tmp_path)
    api._analysis_contexts = {}
    api._files = {}
    api._job_roots = {}
    api._active = {}
    api._queued = main.deque()
    api._workers = {}

    responses = []
    for idx in range(5):
        url = f"https://example.com/v{idx}"
        tok = api._remember_analysis(url, None, 1080)
        jid = f"d{idx:032x}"
        res = api.start_download(jid, url, "1080p (FHD)", "MP4 (호환성 우선)", tok)
        assert res["ok"] is True
        responses.append(res)

    assert sum(1 for r in responses if r.get("queued")) == 2
    gate.set()
    deadline = main.time.monotonic() + 3.0
    while main.time.monotonic() < deadline:
        with api._state_lock:
            if not api._active and not api._queued:
                break
        main.time.sleep(0.02)
    assert len(ran) == 5
