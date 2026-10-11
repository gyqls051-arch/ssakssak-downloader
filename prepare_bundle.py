# -*- coding: utf-8 -*-
"""Build the pinned PO-token side bundle used by the signed installer.

Release builds require ``bundle-release-lock.json``.  The lock pins the Node.js
archive by SHA-256, the npm version contained in that archive, and the bgutil
source by immutable commit.  No PATH-provided Node.js or npm executable is used.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

NODE_VERSION = "22.19.0"
NPM_VERSION = "10.9.3"
NODE_ZIP_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip"
BGUTIL_VERSION = "1.3.1"
BGUTIL_REPO = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"

ROOT = Path(__file__).resolve().parent
DEST = ROOT / "potprovider"
BUILD_TOOLCHAIN = DEST / "_build-toolchain"
MAX_NODE_ZIP_BYTES = 100 * 1024 * 1024
MAX_NODE_UNCOMPRESSED_BYTES = 600 * 1024 * 1024
MAX_NODE_MEMBERS = 50_000
MAX_NPM_BYTES = 300 * 1024 * 1024
MAX_NPM_MEMBERS = 30_000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class BundleLock:
    node_version: str
    node_zip_url: str
    node_zip_sha256: str
    npm_version: str
    bgutil_version: str
    bgutil_repository: str
    bgutil_commit: str


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def _load_release_lock(path: Path) -> BundleLock:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"release bundle lock을 읽을 수 없습니다: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("release bundle lock 최상위는 JSON 객체여야 합니다")
    lock = BundleLock(
        node_version=str(data.get("node_version") or ""),
        node_zip_url=str(data.get("node_zip_url") or ""),
        node_zip_sha256=str(data.get("node_zip_sha256") or "").lower(),
        npm_version=str(data.get("npm_version") or ""),
        bgutil_version=str(data.get("bgutil_version") or ""),
        bgutil_repository=str(data.get("bgutil_repository") or ""),
        bgutil_commit=str(data.get("bgutil_commit") or "").lower(),
    )
    if lock.node_version != NODE_VERSION or lock.node_zip_url != NODE_ZIP_URL:
        raise RuntimeError("release bundle lock의 Node.js version/URL이 소스 상수와 다릅니다")
    if SHA256_RE.fullmatch(lock.node_zip_sha256) is None:
        raise RuntimeError("release bundle lock의 Node.js SHA-256이 올바르지 않습니다")
    if lock.npm_version != NPM_VERSION:
        raise RuntimeError("release bundle lock의 npm version이 Node archive 계약과 다릅니다")
    if lock.bgutil_version != BGUTIL_VERSION or lock.bgutil_repository != BGUTIL_REPO:
        raise RuntimeError("release bundle lock의 bgutil version/repository가 소스 상수와 다릅니다")
    if COMMIT_RE.fullmatch(lock.bgutil_commit) is None:
        raise RuntimeError("release bundle lock의 bgutil commit이 올바르지 않습니다")
    return lock


def _exact_executable(value: str | Path, label: str) -> str:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError(f"{label}의 exact executable을 찾을 수 없습니다: {path}")
    return str(path.resolve())


def run(cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> None:
    """Run a fixed absolute executable with shell expansion disabled."""
    if not cmd:
        raise RuntimeError("빈 build command는 실행할 수 없습니다")
    argv = [_exact_executable(cmd[0], "build command"), *map(str, cmd[1:])]
    log(f"$ {' '.join(argv)}")
    subprocess.run(argv, cwd=str(cwd), check=True, shell=False, env=env)


def command_output(cmd: list[str], cwd: Path, *,
                   env: dict[str, str] | None = None) -> str:
    if not cmd:
        raise RuntimeError("빈 build command는 실행할 수 없습니다")
    argv = [_exact_executable(cmd[0], "build command"), *map(str, cmd[1:])]
    result = subprocess.run(
        argv, cwd=str(cwd), check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace", shell=False, env=env,
    )
    return result.stdout.strip()


def _require_git() -> str:
    resolved = shutil.which("git")
    if not resolved:
        raise RuntimeError("Git이 설치되어 있지 않거나 PATH에서 찾을 수 없습니다")
    return _exact_executable(Path(resolved), "Git")


def _atomic_extract_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo,
                           output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp",
                                   dir=str(output.parent))
    temporary = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as target, archive.open(info) as source:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, output)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _npm_relative_member(info: zipfile.ZipInfo, prefix: str) -> PurePosixPath | None:
    raw = info.filename.replace("\\", "/")
    if not raw.startswith(prefix):
        return None
    if raw == prefix:
        if info.is_dir():
            return None
        raise RuntimeError("Node.js archive npm root가 일반 파일입니다")
    relative = PurePosixPath(raw[len(prefix):])
    mode = (info.external_attr >> 16) & 0xFFFF
    if (not relative.parts or relative.is_absolute()
            or any(part in ("", ".", "..") for part in relative.parts)
            or info.flag_bits & 0x1 or stat.S_ISLNK(mode)):
        raise RuntimeError(f"Node.js archive npm member가 안전하지 않습니다: {raw}")
    return relative


def fetch_node(dest: Path, release_lock: BundleLock) -> None:
    """Verify the pinned archive and extract node.exe plus build-only npm."""
    log(f"Node.js v{NODE_VERSION} 내려받는 중…")
    request = urllib.request.Request(
        NODE_ZIP_URL,
        headers={"User-Agent": "SsakSsakKimchiDownloader-release-builder/1"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        final_url = response.geturl()
        if final_url != NODE_ZIP_URL:
            raise RuntimeError("Node.js archive가 예상하지 않은 URL로 redirect되었습니다")
        raw_length = response.headers.get("Content-Length")
        if raw_length:
            try:
                if int(raw_length) > MAX_NODE_ZIP_BYTES:
                    raise RuntimeError("Node.js archive가 허용 크기를 초과합니다")
            except ValueError as exc:
                raise RuntimeError("Node.js archive Content-Length가 올바르지 않습니다") from exc
        blob = response.read(MAX_NODE_ZIP_BYTES + 1)
    if len(blob) > MAX_NODE_ZIP_BYTES:
        raise RuntimeError("Node.js archive가 허용 크기를 초과합니다")
    if hashlib.sha256(blob).hexdigest() != release_lock.node_zip_sha256:
        raise RuntimeError("Node.js archive SHA-256이 release lock과 다릅니다")

    prefix = f"node-v{NODE_VERSION}-win-x64"
    npm_prefix = f"{prefix}/node_modules/npm/"
    staging_path = Path(tempfile.mkdtemp(prefix=".npm-toolchain-", dir=str(dest)))
    staging: Path | None = staging_path
    npm_output = staging_path / "node_modules" / "npm"
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_NODE_MEMBERS:
                raise RuntimeError("Node.js archive 항목이 비정상적으로 많습니다")
            if sum(info.file_size for info in infos) > MAX_NODE_UNCOMPRESSED_BYTES:
                raise RuntimeError("Node.js archive 압축 해제 크기가 너무 큽니다")

            wanted = {
                f"{prefix}/node.exe": dest / "node.exe",
                f"{prefix}/LICENSE": dest / "NODE-LICENSE.txt",
            }
            for member_name, output in wanted.items():
                try:
                    info = archive.getinfo(member_name)
                except KeyError as exc:
                    raise RuntimeError(
                        f"Node.js archive 필수 파일 누락: {member_name}") from exc
                mode = (info.external_attr >> 16) & 0xFFFF
                if (info.is_dir() or info.file_size <= 0
                        or info.file_size > MAX_NODE_UNCOMPRESSED_BYTES
                        or info.flag_bits & 0x1 or stat.S_ISLNK(mode)):
                    raise RuntimeError(
                        f"Node.js archive member가 비정상입니다: {member_name}")
                _atomic_extract_member(archive, info, output)

            seen: set[PurePosixPath] = set()
            npm_count = 0
            npm_bytes = 0
            for info in infos:
                relative = _npm_relative_member(info, npm_prefix)
                if relative is None:
                    continue
                if relative in seen:
                    raise RuntimeError(f"Node.js archive npm member가 중복됩니다: {relative}")
                seen.add(relative)
                npm_count += 1
                npm_bytes += info.file_size
                if npm_count > MAX_NPM_MEMBERS or npm_bytes > MAX_NPM_BYTES:
                    raise RuntimeError("Node.js archive의 npm payload가 허용 범위를 초과합니다")
                output = npm_output.joinpath(*relative.parts)
                if info.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(output, "xb") as target:
                    shutil.copyfileobj(source, target)
            package_json = npm_output / "package.json"
            npm_cli = npm_output / "bin" / "npm-cli.js"
            if not package_json.is_file() or not npm_cli.is_file():
                raise RuntimeError("Node.js archive에 build용 npm 필수 파일이 없습니다")
            metadata = json.loads(package_json.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict) or metadata.get("version") != release_lock.npm_version:
                raise RuntimeError("Node.js archive의 npm version이 release lock과 다릅니다")

        with open(dest / "node.exe", "rb") as executable:
            if executable.read(2) != b"MZ":
                raise RuntimeError("추출된 node.exe가 Windows PE 파일이 아닙니다")
        shutil.rmtree(BUILD_TOOLCHAIN, ignore_errors=True)
        os.replace(staging_path, BUILD_TOOLCHAIN)
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    log(
        f"node.exe + build-only npm {release_lock.npm_version} 준비 완료 "
        f"({(dest / 'node.exe').stat().st_size / 1048576:.0f} MB)"
    )


def _isolated_npm_env(dest: Path, git_executable: str) -> dict[str, str]:
    toolchain = BUILD_TOOLCHAIN
    cache = toolchain / "npm-cache"
    cache.mkdir(parents=True, exist_ok=True)
    user_config = toolchain / "empty-user.npmrc"
    global_config = toolchain / "empty-global.npmrc"
    user_config.write_text("", encoding="utf-8")
    global_config.write_text("", encoding="utf-8")

    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("NPM_CONFIG_")}
    env.pop("NODE_OPTIONS", None)
    env.pop("NODE_PATH", None)
    path_parts = [str(dest.resolve()), str(Path(git_executable).resolve().parent)]
    if os.name == "nt":
        system_root = Path(env.get("SystemRoot") or env.get("WINDIR") or "C:/Windows")
        path_parts.append(str(system_root / "System32"))
    env.update({
        "PATH": os.pathsep.join(path_parts),
        "NODE": str((dest / "node.exe").resolve()),
        "NPM_CONFIG_CACHE": str(cache.resolve()),
        "NPM_CONFIG_USERCONFIG": str(user_config.resolve()),
        "NPM_CONFIG_GLOBALCONFIG": str(global_config.resolve()),
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NPM_CONFIG_PROGRESS": "false",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def build_server(dest: Path, release_lock: BundleLock, git_executable: str) -> None:
    """Clone the pinned bgutil commit and build with the archive-pinned Node/npm."""
    srv = dest / "server"
    if (srv / "build" / "main.js").exists() and (srv / "node_modules").exists():
        log("server 이미 빌드됨 — 건너뜀")
        return

    node = _exact_executable(dest / "node.exe", "pinned Node.js")
    npm_cli = BUILD_TOOLCHAIN / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if not npm_cli.is_file():
        raise RuntimeError("archive-pinned npm-cli.js를 찾지 못했습니다")
    env = _isolated_npm_env(dest, git_executable)
    if command_output([node, "--version"], cwd=dest, env=env) != f"v{NODE_VERSION}":
        raise RuntimeError("실행 중인 Node.js version이 release lock과 다릅니다")
    if command_output([node, str(npm_cli), "--version"], cwd=dest,
                      env=env) != release_lock.npm_version:
        raise RuntimeError("실행 중인 npm version이 release lock과 다릅니다")

    work = dest / "_src"
    shutil.rmtree(work, ignore_errors=True)
    log(f"bgutil v{BGUTIL_VERSION} 클론 중…")
    run([
        git_executable, "clone", "--depth", "1", "--branch", BGUTIL_VERSION,
        BGUTIL_REPO, str(work),
    ], cwd=dest, env=env)
    actual_commit = command_output(
        [git_executable, "rev-parse", "HEAD"], cwd=work, env=env).lower()
    if actual_commit != release_lock.bgutil_commit:
        raise RuntimeError(
            f"bgutil commit 불일치: expected {release_lock.bgutil_commit}, "
            f"got {actual_commit}"
        )

    src_srv = work / "server"
    if not (src_srv / "package-lock.json").is_file():
        raise RuntimeError("bgutil server package-lock.json이 없습니다")
    log("package-lock 기준 전체 의존성 설치 중…")
    run([
        node, str(npm_cli), "ci", "--no-audit", "--fund=false", "--ignore-scripts",
    ], cwd=src_srv, env=env)
    tsc = src_srv / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc.is_file():
        raise RuntimeError("package-lock 설치본에서 TypeScript compiler를 찾지 못했습니다")
    log("pinned node.exe로 고정 TypeScript compiler 빌드 중…")
    run([node, str(tsc)], cwd=src_srv, env=env)
    log("개발 전용 npm package 제거 중…")
    run([
        node, str(npm_cli), "prune", "--omit=dev", "--no-audit", "--fund=false",
        "--ignore-scripts",
    ], cwd=src_srv, env=env)

    srv.mkdir(parents=True, exist_ok=True)
    for item in ("build", "node_modules", "package.json", "package-lock.json"):
        source = src_srv / item
        target = srv / item
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        shutil.move(str(source), str(target))
    shutil.copy2(work / "LICENSE", srv / "BGUTIL-LICENSE-GPL-3.0.txt")
    log("server 빌드 완료")


def copy_plugin(dest: Path) -> None:
    """Copy the locked yt_dlp_plugins namespace for yt-dlp plugin_dirs."""
    spec = importlib.util.find_spec("yt_dlp_plugins")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(
            "yt_dlp_plugins를 찾을 수 없습니다. release lock 설치를 확인하세요")
    source = Path(list(spec.submodule_search_locations)[0])
    output = dest / "plugin" / "yt_dlp_plugins"
    shutil.rmtree(output.parent, ignore_errors=True)
    shutil.copytree(source, output, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    log(f"플러그인 복사 완료: {source}")


def main() -> int:
    parser = argparse.ArgumentParser(description="고정된 PO-token side bundle 빌드")
    parser.add_argument("--force", action="store_true",
                        help="기존 potprovider/를 지우고 처음부터 만든다")
    parser.add_argument("--release-lock", type=Path, required=True,
                        help="Node/npm과 bgutil source를 고정한 JSON lock")
    args = parser.parse_args()

    lock_path = args.release_lock
    if not lock_path.is_absolute():
        lock_path = ROOT / lock_path
    try:
        release_lock = _load_release_lock(lock_path.resolve())
        git_executable = _require_git()
    except Exception as exc:
        print(f"\n[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.force:
        log("--force: 기존 potprovider/ 삭제")
        shutil.rmtree(DEST, ignore_errors=True)
    DEST.mkdir(parents=True, exist_ok=True)

    try:
        fetch_node(DEST, release_lock)
        build_server(DEST, release_lock, git_executable)
        copy_plugin(DEST)
    except subprocess.CalledProcessError as exc:
        print(
            f"\n[ERROR] 고정 build command 실패 (exit {exc.returncode}). "
            "Git 설치와 release lock을 확인하세요.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"\n[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(BUILD_TOOLCHAIN, ignore_errors=True)
        shutil.rmtree(DEST / "_src", ignore_errors=True)

    size = sum(file.stat().st_size for file in DEST.rglob("*") if file.is_file())
    print(f"\n  완료: {DEST.name}/  ({size / 1048576:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
