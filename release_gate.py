# -*- coding: utf-8 -*-
"""Fail-closed release signing and metadata validation.

This script is intentionally standard-library only.  Release batch files call it
before downloading dependencies, after signing the internal app, from Inno Setup
for Setup/uninstaller signing, and once more for the final canonical artifact.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
APP_EXE_NAME = "SsakSsakKimchiDownloader.exe"
CANONICAL_SETUP_NAME_FMT = "SsakSsakKimchiDownloader-Setup-v{version}.exe"
RELEASE_LOCK = ROOT / "requirements-release-win.lock"
BUNDLE_LOCK = ROOT / "bundle-release-lock.json"
BLOCKER_TOKEN = "RELEASE_BLOCKER"
VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")
SHA1_RE = re.compile(r"^[0-9A-F]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

LEGAL_DOCS = (
    ROOT / "docs" / "EULA.txt",
    ROOT / "docs" / "PRIVACY.md",
    ROOT / "docs" / "THIRD-PARTY-NOTICES.md",
)
RELEASE_INPUTS = LEGAL_DOCS + (
    ROOT / "installer.iss",
    ROOT / "version_info.txt",
)
REQUIRED_LOCK_PACKAGES = frozenset({
    "bgutil-ytdlp-pot-provider",
    "mutagen",
    "pyinstaller",
    "pywebview",
    "yt-dlp",
})


class GateError(RuntimeError):
    """A release policy condition was not met."""


@dataclass(frozen=True)
class ReleaseMetadata:
    app_version: str
    file_version: str
    repository: str
    canonical_setup_name: str
    trusted_thumbprints: frozenset[str]


@dataclass(frozen=True)
class SigningConfig:
    signtool: Path
    thumbprint: str
    timestamp_url: str


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise GateError(f"required file is missing or unreadable: {path.relative_to(ROOT)}") from exc


def _literal_assignment(tree: ast.Module, name: str) -> object:
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id == name and value is not None:
            try:
                return ast.literal_eval(value)
            except (ValueError, TypeError) as exc:
                raise GateError(f"main.py {name} must be a literal value") from exc
    raise GateError(f"main.py does not define {name}")


def _version_parts(value: object) -> tuple[int, ...]:
    version = str(value or "").strip()
    if VERSION_RE.fullmatch(version) is None:
        raise GateError(f"invalid release version: {version!r}")
    parts = tuple(int(part) for part in version.split("."))
    if any(part > 65535 for part in parts):
        raise GateError(f"PE version component exceeds 65535: {version}")
    return parts


def _four_part_version(value: object) -> str:
    parts = _version_parts(value)
    return ".".join(str(part) for part in parts + (0,) * (4 - len(parts)))


def _versions_equal(left: object, right: object) -> bool:
    a, b = _version_parts(left), _version_parts(right)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) == b + (0,) * (width - len(b))


def _normalize_thumbprint(value: object) -> str:
    normalized = re.sub(r"\s+", "", str(value or "")).upper()
    if SHA1_RE.fullmatch(normalized) is None:
        raise GateError("certificate thumbprint must be exactly 40 hexadecimal characters")
    return normalized


def _version_tuple_from_resource(text: str, field: str) -> tuple[int, ...]:
    match = re.search(rf"\b{re.escape(field)}\s*=\s*\(([^)]*)\)", text)
    if match is None:
        raise GateError(f"version_info.txt does not define {field}")
    try:
        return tuple(int(piece.strip()) for piece in match.group(1).split(",")
                     if piece.strip())
    except ValueError as exc:
        raise GateError(f"version_info.txt {field} is not numeric") from exc


def _version_string_from_resource(text: str, field: str) -> str:
    match = re.search(
        rf"StringStruct\(\s*['\"]{re.escape(field)}['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)",
        text,
    )
    if match is None:
        raise GateError(f"version_info.txt does not define StringStruct({field})")
    return match.group(1).strip()


def load_metadata() -> ReleaseMetadata:
    try:
        tree = ast.parse(_read(ROOT / "main.py"), filename="main.py")
    except SyntaxError as exc:
        raise GateError(f"main.py cannot be parsed: {exc}") from exc

    app_version = str(_literal_assignment(tree, "APP_VERSION")).strip()
    _version_parts(app_version)
    required_security_flags = {
        "EXTERNAL_ENGINE_LOADING_ENABLED": False,
        "ENGINE_AUTO_UPDATE_ENABLED": False,
        "REMOTE_KILLSWITCH_ENABLED": False,
        "SECURE_YTDLP_TRANSPORT_ENABLED": True,
    }
    for name, expected in required_security_flags.items():
        if _literal_assignment(tree, name) is not expected:
            raise GateError(f"main.py {name} must be literal {expected!r} for release")
    file_version = _four_part_version(app_version)
    repository = str(_literal_assignment(tree, "GITHUB_RELEASE_REPO")).strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise GateError("main.py GITHUB_RELEASE_REPO is invalid")
    setup_format = _literal_assignment(tree, "CANONICAL_SETUP_NAME_FMT")
    if setup_format != CANONICAL_SETUP_NAME_FMT:
        raise GateError("main.py canonical Setup filename contract changed")

    raw_trusted = _literal_assignment(tree, "TRUSTED_SIGNER_THUMBPRINTS")
    if not isinstance(raw_trusted, (tuple, list)):
        raise GateError("TRUSTED_SIGNER_THUMBPRINTS must be a literal tuple or list")
    trusted: set[str] = set()
    for item in raw_trusted:
        trusted.add(_normalize_thumbprint(item))

    try:
        version_data = json.loads(_read(ROOT / "version.json"))
    except (ValueError, TypeError) as exc:
        raise GateError("version.json is not valid JSON") from exc
    if not isinstance(version_data, dict):
        raise GateError("version.json root must be an object")
    forbidden_manifest_fields = {
        "min_supported_version", "killswitch", "killswitch_message",
        "download_url", "installer_url", "installer_sha256", "installer_size",
    }
    present_forbidden = sorted(forbidden_manifest_fields.intersection(version_data))
    if present_forbidden:
        raise GateError(
            "unsigned version.json contains privileged fields: "
            + ", ".join(present_forbidden)
        )
    published_version = str(version_data.get("latest_version") or "").strip()
    if not _versions_equal(app_version, published_version):
        raise GateError(
            f"version mismatch: main.py={app_version}, version.json={published_version or '<missing>'}"
        )

    resource = _read(ROOT / "version_info.txt")
    expected_tuple = tuple(int(part) for part in file_version.split("."))
    for field in ("filevers", "prodvers"):
        actual = _version_tuple_from_resource(resource, field)
        if actual != expected_tuple:
            raise GateError(
                f"version_info.txt {field}={actual} does not match {expected_tuple}"
            )
    for field in ("FileVersion", "ProductVersion"):
        actual = _version_string_from_resource(resource, field)
        if not _versions_equal(actual, app_version):
            raise GateError(
                f"version_info.txt {field}={actual} does not match {app_version}"
            )

    canonical = CANONICAL_SETUP_NAME_FMT.format(version=app_version)
    return ReleaseMetadata(
        app_version=app_version,
        file_version=file_version,
        repository=repository,
        canonical_setup_name=canonical,
        trusted_thumbprints=frozenset(trusted),
    )


def _logical_lock_entries(text: str) -> Iterable[str]:
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        yield pending
        pending = ""
    if pending:
        yield pending


def _lock_failures() -> list[str]:
    if not RELEASE_LOCK.is_file():
        return [
            "requirements-release-win.lock is missing; generate and review a complete "
            "Windows/Python 3.13 transitive lock with SHA-256 hashes"
        ]
    text = _read(RELEASE_LOCK)
    failures: list[str] = []
    packages: set[str] = set()
    for entry in _logical_lock_entries(text):
        if entry.startswith("--index-url "):
            if entry != "--index-url https://pypi.org/simple":
                failures.append("release lock may only use https://pypi.org/simple")
            continue
        if entry.startswith("--") or entry.startswith(("-e ", "git+", "http://", "https://")):
            failures.append(f"unsupported or mutable release-lock entry: {entry[:100]}")
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", entry)
        if match is None:
            failures.append(f"release-lock entry is not exactly pinned: {entry[:100]}")
            continue
        package = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        packages.add(package)
        hashes = re.findall(r"--hash=sha256:([0-9a-fA-F]{64})", entry)
        if not hashes or any(SHA256_RE.fullmatch(value.lower()) is None for value in hashes):
            failures.append(f"release-lock entry has no valid SHA-256 hash: {package}")
    missing = sorted(REQUIRED_LOCK_PACKAGES - packages)
    if missing:
        failures.append("release lock is missing required packages: " + ", ".join(missing))
    return failures


def _bundle_lock_failures() -> list[str]:
    if not BUNDLE_LOCK.is_file():
        return ["bundle-release-lock.json is missing"]
    try:
        data = json.loads(_read(BUNDLE_LOCK))
    except (ValueError, TypeError):
        return ["bundle-release-lock.json is not valid JSON"]
    if not isinstance(data, dict):
        return ["bundle-release-lock.json root must be an object"]
    failures: list[str] = []
    expected_url = (
        f"https://nodejs.org/dist/v{data.get('node_version', '')}/"
        f"node-v{data.get('node_version', '')}-win-x64.zip"
    )
    if data.get("node_zip_url") != expected_url:
        failures.append("bundle lock Node.js URL must be the exact versioned nodejs.org win-x64 archive")
    if SHA256_RE.fullmatch(str(data.get("node_zip_sha256") or "").lower()) is None:
        failures.append("bundle lock Node.js archive requires a 64-character SHA-256")
    npm_version = str(data.get("npm_version") or "")
    if VERSION_RE.fullmatch(npm_version) is None:
        failures.append("bundle lock requires an exact npm version from the pinned Node archive")
    if data.get("bgutil_repository") != "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git":
        failures.append("bundle lock bgutil repository is not the approved exact URL")
    if re.fullmatch(r"[0-9a-f]{40}", str(data.get("bgutil_commit") or "").lower()) is None:
        failures.append("bundle lock bgutil source requires an immutable 40-character commit")
    source = _read(ROOT / "prepare_bundle.py")
    expected_source_fragments = (
        f'NODE_VERSION = "{data.get("node_version", "")}"',
        f'NPM_VERSION = "{data.get("npm_version", "")}"',
        'NODE_ZIP_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip"',
        f'BGUTIL_VERSION = "{data.get("bgutil_version", "")}"',
        f'BGUTIL_REPO = "{data.get("bgutil_repository", "")}"',
    )
    for fragment in expected_source_fragments:
        if fragment not in source:
            failures.append(f"bundle lock does not match prepare_bundle.py: {fragment}")
    for forbidden in ('run(["npm"', 'run(["node"', 'node_modules" / ".bin"',
                      'ENGINE = ROOT / "engine"', "def build_engine"):
        if forbidden in source:
            failures.append(
                f"prepare_bundle.py still permits an unpinned/external toolchain path: {forbidden}")
    required_toolchain_fragments = (
        'node, str(npm_cli), "ci"',
        'node, str(tsc)',
        'node, str(npm_cli), "prune"',
        'NPM_CONFIG_USERCONFIG',
        'NPM_CONFIG_CACHE',
    )
    for fragment in required_toolchain_fragments:
        if fragment not in source:
            failures.append(f"prepare_bundle.py is missing pinned toolchain contract: {fragment}")
    return failures


def _ffmpeg_pin_failures() -> list[str]:
    """Require immutable archive and extracted executable pins before release."""
    source = _read(ROOT / "main.py")
    try:
        tree = ast.parse(source, filename="main.py")
        tag = str(_literal_assignment(tree, "FFMPEG_RELEASE_TAG") or "").strip()
        name = str(_literal_assignment(tree, "FFMPEG_ASSET_NAME") or "").strip()
        url = str(_literal_assignment(tree, "FFMPEG_ARCHIVE_URL") or "").strip()
        archive_size = _literal_assignment(tree, "FFMPEG_ARCHIVE_SIZE")
        archive_hash = str(
            _literal_assignment(tree, "FFMPEG_ARCHIVE_SHA256") or "").lower()
        executable_size = _literal_assignment(tree, "FFMPEG_EXECUTABLE_SIZE")
        executable_hash = str(
            _literal_assignment(tree, "FFMPEG_EXECUTABLE_SHA256") or "").lower()
    except (GateError, SyntaxError) as exc:
        return [f"ffmpeg release pin cannot be parsed: {exc}"]

    failures: list[str] = []
    if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag) is None
            or tag.lower() == "latest"):
        failures.append("main.py requires an immutable non-latest FFmpeg release tag")
    if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,255}\.zip", name) is None
            or Path(name).name != name):
        failures.append("main.py requires an exact FFmpeg ZIP asset name")
    expected_url = (
        f"https://github.com/BtbN/FFmpeg-Builds/releases/download/{tag}/{name}"
    )
    if url != expected_url:
        failures.append("main.py FFmpeg URL must exactly match the immutable tag and asset")
    if not isinstance(archive_size, int) or archive_size <= 0:
        failures.append("main.py FFmpeg ZIP size pin is missing")
    if SHA256_RE.fullmatch(archive_hash) is None:
        failures.append("main.py FFmpeg ZIP SHA-256 pin is missing")
    if not isinstance(executable_size, int) or executable_size <= 0:
        failures.append("main.py extracted FFmpeg EXE size pin is missing")
    if SHA256_RE.fullmatch(executable_hash) is None:
        failures.append("main.py extracted FFmpeg EXE SHA-256 pin is missing")
    for forbidden in ("_fetch_asset_metadata", 'RELEASE_TAG = "latest"',
                      'which("ffmpeg")'):
        if forbidden in source:
            failures.append(f"main.py still contains mutable/unpinned FFmpeg trust: {forbidden}")
    return failures


def static_failures() -> tuple[ReleaseMetadata | None, list[str]]:
    failures: list[str] = []
    try:
        metadata = load_metadata()
    except GateError as exc:
        return None, [str(exc)]

    if not metadata.trusted_thumbprints:
        failures.append(
            "main.py TRUSTED_SIGNER_THUMBPRINTS is empty; automatic updates and release builds stay blocked"
        )

    for path in LEGAL_DOCS:
        if not path.is_file():
            failures.append(f"required release document is missing: {path.relative_to(ROOT)}")
    for path in RELEASE_INPUTS:
        if path.is_file() and BLOCKER_TOKEN in _read(path):
            failures.append(
                f"unresolved legal/release placeholder in {path.relative_to(ROOT)} ({BLOCKER_TOKEN})"
            )

    installer = _read(ROOT / "installer.iss")
    compact_installer = re.sub(r"[ \t]", "", installer)
    required_installer_fragments = (
        "#ifndefMyAppVersion",
        "#ifndefMyFileVersion",
        "OutputDir=dist",
        "OutputBaseFilename=SsakSsakKimchiDownloader-Setup-v{#MyAppVersion}",
        "VersionInfoVersion={#MyFileVersion}",
        "VersionInfoProductVersion={#MyFileVersion}",
        "VersionInfoProductTextVersion={#MyAppVersion}",
        "SignTool=release",
        "SignedUninstaller=yes",
        'Source:"build\\app\\{#MyAppExeName}"',
        "LicenseFile=docs\\EULA.txt",
    )
    for fragment in required_installer_fragments:
        if fragment not in compact_installer:
            failures.append(f"installer.iss is missing release contract: {fragment}")
    if re.search(r"^\s*#define\s+MyAppVersion\b", installer, re.MULTILINE):
        failures.append("installer.iss must receive MyAppVersion from the validated build command")
    if re.search(r"^\s*#define\s+MyFileVersion\b", installer, re.MULTILINE):
        failures.append("installer.iss must receive MyFileVersion from the validated build command")
    if re.search(r'^\s*Source:\s*"engine[\\/]', installer, re.MULTILINE | re.IGNORECASE):
        failures.append("installer.iss must not install a writable external yt-dlp engine")

    build_script = _read(ROOT / "build.bat")
    for forbidden in ("--collect-all curl_cffi", "potprovider\\ and engine\\",
                      "potprovider\\ / engine\\"):
        if forbidden in build_script:
            failures.append(f"build.bat still contains removed runtime input: {forbidden}")

    web = _read(ROOT / "web" / "index.html")
    expected_url = (
        f"https://github.com/{metadata.repository}/releases/download/"
        f"v{metadata.app_version}/{metadata.canonical_setup_name}"
    )
    if expected_url not in web:
        failures.append(f"web/index.html does not link the canonical v{metadata.app_version} Setup")
    for forbidden in (
        "releases/latest/download/SsakSsakKimchiDownloader.exe",
        "설치 불필요",
        "약 41MB",
        "추가 정보 → 실행",
        "서명되지 않은 개인 개발 프로그램",
    ):
        if forbidden in web:
            failures.append(f"web/index.html still contains unsafe/stale guidance: {forbidden}")

    readme = _read(ROOT / "README.md")
    if metadata.canonical_setup_name not in readme:
        failures.append("README.md does not name the current canonical Setup")
    if "dist/SsakSsakKimchiDownloader.exe" in readme:
        failures.append("README.md still presents the raw app EXE as a distribution artifact")

    requirements = _read(ROOT / "requirements.txt")
    if "prepare_potprovider.py" in requirements:
        failures.append("requirements.txt still references nonexistent prepare_potprovider.py")
    if "ENABLE_PLAYWRIGHT = not frozen" in requirements:
        failures.append("requirements.txt still claims Playwright is automatically enabled")

    ui = _read(ROOT / "web_ui" / "index.html")
    for csp_fragment in (
        "default-src 'none'", "img-src 'self' data: blob:",
        "connect-src 'none'", "object-src 'none'", "frame-src 'none'",
    ):
        if csp_fragment not in ui:
            failures.append(f"web_ui CSP is missing release boundary: {csp_fragment}")

    failures.extend(_lock_failures())
    failures.extend(_bundle_lock_failures())
    failures.extend(_ffmpeg_pin_failures())
    return metadata, failures


def _strip_outer_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def signing_config(metadata: ReleaseMetadata) -> tuple[SigningConfig | None, list[str]]:
    failures: list[str] = []
    raw_tool = _strip_outer_quotes(os.environ.get("SIGNTOOL_EXE", ""))
    raw_thumbprint = os.environ.get("SIGN_CERT_SHA1", "")
    timestamp_url = os.environ.get("SIGN_TIMESTAMP_URL", "").strip()

    tool = Path(raw_tool).expanduser() if raw_tool else Path()
    if not raw_tool:
        failures.append("SIGNTOOL_EXE is not set")
    elif not tool.is_file():
        failures.append("SIGNTOOL_EXE does not point to an existing file")

    try:
        thumbprint = _normalize_thumbprint(raw_thumbprint)
    except GateError as exc:
        thumbprint = ""
        failures.append(f"SIGN_CERT_SHA1: {exc}")
    if thumbprint and thumbprint not in metadata.trusted_thumbprints:
        failures.append("SIGN_CERT_SHA1 is not present in main.py TRUSTED_SIGNER_THUMBPRINTS")

    try:
        parsed = urllib.parse.urlsplit(timestamp_url)
        port = parsed.port
    except ValueError:
        parsed, port = urllib.parse.SplitResult("", "", "", "", ""), None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and port != 443)
        or any(ord(char) < 32 for char in timestamp_url)
    ):
        failures.append("SIGN_TIMESTAMP_URL must be a credential-free HTTPS URL on port 443")

    if failures:
        return None, failures
    return SigningConfig(tool.resolve(), thumbprint, timestamp_url), []


def _print_failures(title: str, failures: Iterable[str]) -> int:
    items = list(dict.fromkeys(failures))
    if not items:
        print(f"[OK] {title}")
        return 0
    print(f"[BLOCKED] {title}", file=sys.stderr)
    for item in items:
        print(f"  - {item}", file=sys.stderr)
    return 2


def run_static_check() -> int:
    _, failures = static_failures()
    return _print_failures("static release contract", failures)


def run_preflight() -> tuple[ReleaseMetadata, SigningConfig] | None:
    metadata, failures = static_failures()
    if metadata is None:
        _print_failures("release preflight", failures)
        return None
    if os.name != "nt":
        failures.append("commercial release builds must run on Windows")
    signer, signer_failures = signing_config(metadata)
    failures.extend(signer_failures)
    if _print_failures("release preflight", failures) != 0 or signer is None:
        return None
    return metadata, signer


def _resolve_file(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise GateError(f"file does not exist: {path}") from exc
    if not resolved.is_file():
        raise GateError(f"not a regular file: {resolved}")
    return resolved


def _run_signtool_verify(path: Path, config: SigningConfig) -> None:
    result = subprocess.run(
        [str(config.signtool), "verify", "/pa", "/all", "/v", str(path)],
        check=False,
    )
    if result.returncode != 0:
        raise GateError(f"SignTool verification failed for {path.name}")


def _authenticode_info(path: Path) -> dict[str, str]:
    script = (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
        "$p=$env:SSAK_RELEASE_FILE;"
        "$s=Get-AuthenticodeSignature -LiteralPath $p;"
        "$f=[Diagnostics.FileVersionInfo]::GetVersionInfo($p);"
        "$thumb='';if($s.SignerCertificate){$thumb=$s.SignerCertificate.Thumbprint};"
        "$subject='';if($s.SignerCertificate){$subject=$s.SignerCertificate.Subject};"
        "$timestamp='';if($s.TimeStamperCertificate){$timestamp=$s.TimeStamperCertificate.Thumbprint};"
        "[ordered]@{Status=[string]$s.Status;Thumbprint=[string]$thumb;"
        "Subject=[string]$subject;TimestampThumbprint=[string]$timestamp;"
        "ProductVersion=[string]$f.ProductVersion;FileVersion=[string]$f.FileVersion}"
        "|ConvertTo-Json -Compress"
    )
    env = os.environ.copy()
    env["SSAK_RELEASE_FILE"] = str(path)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError("PowerShell Authenticode verification could not run") from exc
    if result.returncode != 0:
        raise GateError("PowerShell Authenticode verification failed")
    try:
        data = json.loads(result.stdout.strip())
    except (ValueError, TypeError) as exc:
        raise GateError("PowerShell Authenticode result was not valid JSON") from exc
    if not isinstance(data, dict):
        raise GateError("PowerShell Authenticode result was not an object")
    return {str(key): str(value or "").strip() for key, value in data.items()}


def verify_signature(path: Path, metadata: ReleaseMetadata,
                     config: SigningConfig, expected_version: str | None) -> dict[str, str]:
    with path.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise GateError(f"not a Windows PE executable: {path.name}")
    _run_signtool_verify(path, config)
    info = _authenticode_info(path)
    if info.get("Status") != "Valid":
        raise GateError(f"Authenticode status is not Valid: {info.get('Status') or '<empty>'}")
    actual_thumbprint = re.sub(r"\s+", "", info.get("Thumbprint", "")).upper()
    if actual_thumbprint != config.thumbprint:
        raise GateError("signed file certificate does not match SIGN_CERT_SHA1")
    if actual_thumbprint not in metadata.trusted_thumbprints:
        raise GateError("signed file certificate is not trusted by main.py")
    if not info.get("TimestampThumbprint"):
        raise GateError("signed file has no trusted timestamp certificate")
    if expected_version is not None:
        product_version = info.get("ProductVersion", "")
        if not _versions_equal(product_version, expected_version):
            raise GateError(
                f"ProductVersion mismatch: expected {expected_version}, got {product_version or '<empty>'}"
            )
    return info


def sign_file(path: Path, metadata: ReleaseMetadata, config: SigningConfig) -> None:
    result = subprocess.run(
        [
            str(config.signtool), "sign",
            "/sha1", config.thumbprint,
            "/fd", "SHA256",
            "/tr", config.timestamp_url,
            "/td", "SHA256",
            "/d", "SsakSsak Kimchi Downloader",
            "/v",
            str(path),
        ],
        check=False,
    )
    if result.returncode != 0:
        raise GateError(f"SignTool signing failed for {path.name}")
    verify_signature(path, metadata, config, expected_version=None)
    print(f"[OK] signed and verified: {path}")


def verify_release_file(path: Path, kind: str, metadata: ReleaseMetadata,
                        config: SigningConfig) -> None:
    expected_name = APP_EXE_NAME if kind == "app" else metadata.canonical_setup_name
    if path.name != expected_name:
        raise GateError(f"unexpected {kind} filename: {path.name}; expected {expected_name}")
    info = verify_signature(path, metadata, config, metadata.app_version)
    print(
        f"[OK] verified {kind}: {path.name} | ProductVersion={info.get('ProductVersion')} "
        f"| signer={info.get('Subject') or config.thumbprint}"
    )


def finalize(path: Path, metadata: ReleaseMetadata, config: SigningConfig) -> None:
    verify_release_file(path, "setup", metadata, config)
    dist = ROOT / "dist"
    executable_names = sorted(item.name for item in dist.glob("*.exe") if item.is_file())
    if executable_names != [metadata.canonical_setup_name]:
        raise GateError(
            "dist must contain exactly one executable, the canonical Setup; found: "
            + (", ".join(executable_names) or "none")
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"RELEASE_FILE={path.name}")
    print(f"RELEASE_SIZE={path.stat().st_size}")
    print(f"RELEASE_SHA256={digest}")
    print("[OK] canonical release artifact is ready; publish version.json only after upload verification")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Commercial release fail-closed gate")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("metadata", help="emit validated version values for batch scripts")
    sub.add_parser("check", help="check source release contract without Windows signing tools")
    sub.add_parser("preflight", help="check source, legal, lock, OS, and signer configuration")
    sign = sub.add_parser("sign", help="Authenticode-sign one Inno/PyInstaller file")
    sign.add_argument("--file", required=True)
    verify = sub.add_parser("verify", help="verify one signed app or Setup")
    verify.add_argument("--file", required=True)
    verify.add_argument("--kind", required=True, choices=("app", "setup"))
    final = sub.add_parser("finalize", help="verify the sole canonical dist artifact")
    final.add_argument("--file", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "metadata":
            metadata = load_metadata()
            print(f"APP_VERSION={metadata.app_version}")
            print(f"FILE_VERSION={metadata.file_version}")
            print(f"CANONICAL_SETUP_NAME={metadata.canonical_setup_name}")
            print(f"APP_EXE_NAME={APP_EXE_NAME}")
            return 0
        if args.command == "check":
            return run_static_check()

        ready = run_preflight()
        if ready is None:
            return 2
        metadata, config = ready
        if args.command == "preflight":
            return 0
        path = _resolve_file(args.file)
        if args.command == "sign":
            sign_file(path, metadata, config)
        elif args.command == "verify":
            verify_release_file(path, args.kind, metadata, config)
        elif args.command == "finalize":
            finalize(path, metadata, config)
        return 0
    except GateError as exc:
        print(f"[BLOCKED] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
