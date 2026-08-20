# -*- coding: utf-8 -*-
"""빌드 준비: 배포에 필요한 곁폴더 두 개(potprovider/, engine/)를 만든다.

왜 필요한가
----------
유튜브가 SABR 스트리밍을 강제하면서, PO Token 없이는 어떤 재생 클라이언트로도
실제 스트림을 받을 수 없다(포맷 목록이 비거나, 받아도 10MB 즈음 403 Forbidden).
토큰 생성에는 브라우저 BotGuard 로직을 돌릴 JS 런타임이 필요해서 Node.js 와
bgutil 서버를 함께 배포한다.

만들어지는 구조 (exe 옆에 그대로 설치됨)
    potprovider/
        node.exe                  ← Node.js 런타임 (단일 실행 파일)
        server/build/main.js      ← 토큰 생성 HTTP 서버 (TS 컴파일 결과)
        server/node_modules/      ← 서버 의존성 (프로덕션만)
        plugin/yt_dlp_plugins/    ← yt-dlp 플러그인 (pip 설치 없이 폴더로 로드)

또 하나: engine/ — yt-dlp 본체
    exe 안에만 두면 유튜브가 바뀔 때마다 사용자가 설치본을 통째로 다시 받아야
    한다(버전이 '되냐'뿐 아니라 '화질'까지 좌우한다: 2026.03 → 360p,
    2026.08 → 1080p). 그래서 엔진을 폴더로 분리해 앱이 스스로 갱신하게 한다.
    engine/
        yt_dlp/                ← pip 로 설치된 yt-dlp 를 그대로 복사
        engine_version.txt     ← 버전 표기 (앱이 최신본 고를 때 비교)

사용법:  py -3.13 prepare_bundle.py           (build.bat 이 자동 호출)
         py -3.13 prepare_bundle.py --force   (기존 폴더 지우고 새로)
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

# Node.js 는 LTS 로 고정한다(bgutil 서버가 node>=22 요구).
NODE_VERSION = "22.19.0"
NODE_ZIP_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip"

# 서버(JS)와 플러그인(pip) 은 버전이 맞아야 한다. requirements.txt 의
# bgutil-ytdlp-pot-provider 와 아래 태그를 함께 올릴 것.
BGUTIL_VERSION = "1.3.1"
BGUTIL_REPO = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"

ROOT = Path(__file__).resolve().parent
DEST = ROOT / "potprovider"
ENGINE = ROOT / "engine"
ENGINE_VERSION_FILE = "engine_version.txt"    # main.py 의 같은 이름 상수와 일치할 것


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def run(cmd: list[str], cwd: Path) -> None:
    """실패하면 예외. npm/npx 는 Windows 에서 .cmd 라 shell=True 가 필요하다."""
    log(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(cwd), check=True,
                   shell=(os.name == "nt"))


def fetch_node(dest: Path) -> None:
    """node.exe 하나만 받아서 둔다 (전체 배포판에서 실행 파일만 추출)."""
    if (dest / "node.exe").exists():
        log("node.exe 이미 있음 — 건너뜀")
        return
    log(f"Node.js v{NODE_VERSION} 내려받는 중…")
    with urllib.request.urlopen(NODE_ZIP_URL, timeout=180) as resp:
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        member = f"node-v{NODE_VERSION}-win-x64/node.exe"
        with z.open(member) as src, open(dest / "node.exe", "wb") as out:
            shutil.copyfileobj(src, out)
    log(f"node.exe 준비 완료 ({(dest / 'node.exe').stat().st_size / 1048576:.0f} MB)")


def build_server(dest: Path) -> None:
    """bgutil 서버를 받아 프로덕션 의존성만 설치하고 TS 를 컴파일한다."""
    srv = dest / "server"
    if (srv / "build" / "main.js").exists() and (srv / "node_modules").exists():
        log("server 이미 빌드됨 — 건너뜀")
        return

    work = dest / "_src"
    shutil.rmtree(work, ignore_errors=True)
    log(f"bgutil v{BGUTIL_VERSION} 클론 중…")
    run(["git", "clone", "--depth", "1", "--branch", BGUTIL_VERSION,
         BGUTIL_REPO, str(work)], cwd=dest)

    src_srv = work / "server"
    log("의존성 설치 중 (프로덕션만)…")
    run(["npm", "ci", "--omit=dev"], cwd=src_srv)
    log("TypeScript 컴파일 중…")
    # tsc 는 devDependency 라 --omit=dev 설치본엔 없다. 일회성으로 받아 쓴다.
    run(["npx", "-y", "typescript@5.9.3", "tsc"], cwd=src_srv)

    srv.mkdir(parents=True, exist_ok=True)
    for item in ("build", "node_modules", "package.json"):
        s = src_srv / item
        d = srv / item
        if d.exists():
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        shutil.move(str(s), str(d))
    shutil.rmtree(work, ignore_errors=True)
    log("server 빌드 완료")


def copy_plugin(dest: Path) -> None:
    """pip 로 설치된 yt_dlp_plugins 패키지를 폴더째 복사한다.

    PyInstaller 배포본에선 네임스페이스 패키지 자동탐색이 안 되므로,
    앱이 plugin_dirs 로 직접 가리킬 수 있게 파일로 둔다."""
    spec = importlib.util.find_spec("yt_dlp_plugins")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit(
            "[ERROR] yt_dlp_plugins 를 찾을 수 없습니다.\n"
            "        py -3.13 -m pip install -r requirements.txt 를 먼저 실행하세요.")
    src = Path(list(spec.submodule_search_locations)[0])
    out = dest / "plugin" / "yt_dlp_plugins"
    shutil.rmtree(out.parent, ignore_errors=True)
    shutil.copytree(src, out,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    log(f"플러그인 복사 완료: {src}")


def build_engine() -> None:
    """설치된 yt-dlp 를 engine/ 으로 복사하고 버전을 적어 둔다."""
    import yt_dlp                                   # requirements 설치 후에 존재
    src = Path(yt_dlp.__file__).resolve().parent
    version = yt_dlp.version.__version__
    shutil.rmtree(ENGINE, ignore_errors=True)
    ENGINE.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, ENGINE / "yt_dlp",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (ENGINE / ENGINE_VERSION_FILE).write_text(version, encoding="utf-8")
    log(f"engine 준비 완료: yt-dlp {version}")


def main() -> int:
    ap = argparse.ArgumentParser(description="배포용 곁폴더 빌드 준비")
    ap.add_argument("--force", action="store_true",
                    help="기존 potprovider/ 를 지우고 처음부터 다시 만든다")
    args = ap.parse_args()

    if args.force:
        log("--force: 기존 potprovider/ · engine/ 삭제")
        shutil.rmtree(DEST, ignore_errors=True)
        shutil.rmtree(ENGINE, ignore_errors=True)
    DEST.mkdir(parents=True, exist_ok=True)

    try:
        fetch_node(DEST)
        build_server(DEST)
        copy_plugin(DEST)
        build_engine()
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] 명령 실패 (exit {e.returncode}). "
              f"git / node / npm 이 PATH 에 있는지 확인하세요.", file=sys.stderr)
        return 1
    except Exception as e:                       # noqa: BLE001
        print(f"\n[ERROR] {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print()
    for folder in (DEST, ENGINE):
        size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        print(f"  완료: {folder.name}/  ({size / 1048576:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
