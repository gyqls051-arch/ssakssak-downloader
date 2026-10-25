"""Publisher-authenticated whole-app updates; never execute an external Python engine."""
from __future__ import annotations
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PRODUCT = 'SsakSsakKimchiDownloader'
REPOSITORY = 'gyqls051-arch/ssakssak-downloader'
PUBLIC_KEY = '5d9364edec6f4771e015aae0837e781ec3646fbdbaa984ea3ce7e0e853bc5f61'
CHANNEL_URL = f'https://raw.githubusercontent.com/{REPOSITORY}/main/updates/stable.json'
MANIFEST_LIMIT = 16384
MAX_SIZE = 400 * 1024 * 1024
CHECK_SECONDS = 12 * 3600


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def sign_manifest(body, key):
    signature = base64.b64encode(key.sign(canonical(body))).decode('ascii')
    return canonical({'payload': body, 'signature': signature})


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result: raise ValueError('Duplicate manifest field')
        result[name] = value
    return result


def version_tuple(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9]{1,5}(?:\.[0-9]{1,5}){1,3}', value) is None:
        raise ValueError('Invalid version')
    parts = tuple(int(n) for n in value.split('.'))
    if any(n > 65535 for n in parts): raise ValueError('Invalid Windows version')
    return parts + (0,) * (4 - len(parts))


@dataclass(frozen=True)
class VerifiedRelease:
    version: str
    size: int
    sha256: str
    raw: bytes
    issued: int

    @property
    def name(self): return f'{PRODUCT}-Setup-v{self.version}.exe'

    @property
    def url(self): return f'https://github.com/{REPOSITORY}/releases/download/v{self.version}/{self.name}'


def verify_manifest(raw, current, *, minimum=None, public_key=PUBLIC_KEY, now=None):
    if not isinstance(raw, bytes) or len(raw) > MANIFEST_LIMIT: raise ValueError('Manifest too large')
    try:
        envelope = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        if set(envelope) != {'payload', 'signature'}: raise ValueError('Invalid envelope')
        body = envelope['payload']
        signature = base64.b64decode(envelope['signature'], validate=True)
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(signature, canonical(body))
    except (InvalidSignature, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError('Publisher signature rejected') from exc
    if not isinstance(body, dict) or set(body) != {'schema','product','version','issued','expires','size','sha256','source'}:
        raise ValueError('Invalid manifest fields')
    clock = int(time.time()) if now is None else now
    if type(body['schema']) is not int or body['schema'] != 1 or body['product'] != PRODUCT:
        raise ValueError('Wrong update product/schema')
    issued, expires = body['issued'], body['expires']
    if type(issued) is not int or type(expires) is not int or not issued <= clock + 300 or not clock < expires <= issued + 8*86400:
        raise ValueError('Expired or future manifest')
    version = body['version']
    if version_tuple(version) <= version_tuple(current): raise ValueError('Update is not newer')
    if minimum and version_tuple(version) < version_tuple(minimum): raise ValueError('Update replay rejected')
    size = body['size']
    if type(size) is not int or not 5*1024*1024 <= size <= MAX_SIZE: raise ValueError('Invalid update size')
    if not isinstance(body['sha256'], str) or re.fullmatch('[a-f0-9]{64}',body['sha256']) is None:
        raise ValueError('Invalid update digest')
    if not isinstance(body['source'], str) or re.fullmatch('[a-f0-9]{40}',body['source']) is None:
        raise ValueError('Invalid source revision')
    return VerifiedRelease(version, size, body['sha256'], raw, issued)


def verify_file(path, release):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size != release.size:
        raise ValueError('Update size/path mismatch')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ': raise ValueError('Update is not a PE file')
        stream.seek(0)
        for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
    if digest.hexdigest() != release.sha256: raise ValueError('Update digest mismatch')


def atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class PublisherUpdater:
    def __init__(self, current, root, open_url, *, public_key=PUBLIC_KEY):
        self.current, self.root, self.open_url, self.public_key = current, Path(root), open_url, public_key

    def state(self):
        try:
            value = json.loads((self.root/'state.json').read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError): return {}

    def check(self):
        with self.open_url(CHANNEL_URL) as response: raw = response.read(MANIFEST_LIMIT+1)
        try:
            release = verify_manifest(raw, self.current, minimum=self.state().get('highest'), public_key=self.public_key)
        except ValueError as exc:
            # A valid current release means there is simply nothing to install.
            if str(exc) == 'Update is not newer': return None
            raise
        state = self.state()
        if state.get('failed') == release.version: return None
        state['highest'] = release.version
        atomic_json(self.root/'state.json', state)
        return release

    def download(self, release, cancelled=lambda: False):
        self.root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix='verified-', dir=self.root))
        target = directory/release.name
        started = time.monotonic()
        try:
            with self.open_url(release.url) as response, target.open('xb') as output:
                total = 0
                while True:
                    if cancelled() or time.monotonic()-started > 300: raise ValueError('Update cancelled/timed out')
                    chunk = response.read(256*1024)
                    if not chunk: break
                    total += len(chunk)
                    if total > release.size: raise ValueError('Oversized update')
                    output.write(chunk)
                output.flush(); os.fsync(output.fileno())
            verify_file(target, release)
            # Expiry and signature are checked again immediately before execution too.
            return target
        except Exception:
            target.unlink(missing_ok=True)
            directory.rmdir()
            raise

    def launch_helper(self, setup, release, executable):
        release = verify_manifest(release.raw, self.current, minimum=self.state().get('highest'), public_key=self.public_key)
        verify_file(setup, release)
        executable = Path(executable).resolve(strict=True)
        if executable.name != PRODUCT+'.exe': raise ValueError('Unexpected installed executable')
        directory = Path(setup).parent
        ticket = directory/'ticket.json'
        atomic_json(ticket, {'manifest':json.loads(release.raw), 'current':self.current,
                             'install':str(executable.parent), 'parent':os.getpid()})
        helper = directory/'update-helper.exe'
        shutil.copyfile(executable, helper)
        env = os.environ.copy(); env['PYINSTALLER_RESET_ENVIRONMENT']='1'
        process=subprocess.Popen([str(helper),'--apply-publisher-update',str(ticket)],env=env,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),close_fds=True)
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if (directory/'ready').is_file(): return
            if process.poll() is not None: raise RuntimeError('Update helper could not start')
            time.sleep(.1)
        process.terminate()
        raise RuntimeError('Update helper readiness timed out')


@contextlib.contextmanager
def deny_write_lock(path):
    """Prevent replacing a verified installer while Windows opens its image."""
    if os.name != 'nt':
        with Path(path).open('rb'): yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle = kernel.CreateFileW(str(Path(path).resolve()),0x80000000,1,None,3,0x200000,None)
    if handle == wintypes.HANDLE(-1).value: raise OSError('Cannot lock verified update')
    try: yield
    finally: kernel.CloseHandle(handle)


def apply_ticket(ticket_path):
    """Owned detached helper: wait, verify, back up owned files, install, probe, rollback."""
    import ctypes
    ticket_path=Path(ticket_path).resolve(strict=True)
    ticket=json.loads(ticket_path.read_text(encoding='utf-8'))
    release=verify_manifest(canonical(ticket['manifest']),ticket['current'])
    install=Path(ticket['install'])
    if not install.is_absolute() or install.is_symlink(): raise ValueError('Invalid install directory')
    exe=install/(PRODUCT+'.exe')
    if not exe.is_file() or exe.is_symlink(): raise ValueError('Missing original application')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.restype=ctypes.c_void_p
    parent=kernel.OpenProcess(0x100000|0x1000,False,int(ticket['parent']))
    if not parent: raise ValueError('Update parent is unavailable')
    try:
        size=ctypes.c_ulong(32768); image=ctypes.create_unicode_buffer(size.value)
        kernel.QueryFullProcessImageNameW.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_ulong)]
        if not kernel.QueryFullProcessImageNameW(parent,0,image,ctypes.byref(size)) or Path(image.value).resolve()!=exe.resolve():
            raise ValueError('Update parent does not own this installation')
        (ticket_path.parent/'ready').write_text('ready',encoding='ascii')
        kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
        if kernel.WaitForSingleObject(parent,30000)!=0: raise ValueError('Application did not become idle/exit')
    finally:
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]; kernel.CloseHandle(parent)
    setup=ticket_path.parent/release.name
    backup=ticket_path.parent/'previous'; backup.mkdir()
    owned=[PRODUCT+'.exe','potprovider','docs']
    env=os.environ.copy(); env['PYINSTALLER_RESET_ENVIRONMENT']='1'
    state_path=ticket_path.parent.parent/'state.json'
    try:
        for name in owned:
            source=install/name
            for entry in [source, *source.rglob('*')] if source.is_dir() else [source]:
                if entry.is_symlink() or (entry.exists() and getattr(entry.stat(),'st_file_attributes',0)&1024):
                    raise ValueError('Reparse point in installed payload')
            if source.is_dir(): shutil.copytree(source,backup/name)
            elif source.is_file(): shutil.copy2(source,backup/name)
        with deny_write_lock(setup):
            release=verify_manifest(release.raw,ticket['current'])
            verify_file(setup,release)
            result=subprocess.run([str(setup),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
                                   '/CLOSEAPPLICATIONS','/AUTORUN=0','/DIR='+str(install)],timeout=300)
        if result.returncode!=0: raise RuntimeError('Installer failed: '+str(result.returncode))
        probe=subprocess.run([str(exe),'--self-test'],env=env,capture_output=True,text=True,timeout=45)
        if probe.returncode!=0 or json.loads(probe.stdout).get('version')!=release.version:
            raise RuntimeError('Updated application failed its startup probe')
        atomic_json(state_path,{'highest':release.version,'installed':release.version})
    except Exception as exc:
        for name in owned:
            source=backup/name
            if source.is_dir(): shutil.copytree(source,install/name,dirs_exist_ok=True)
            elif source.is_file(): shutil.copy2(source,install/name)
        atomic_json(state_path,{'highest':release.version,'failed':release.version,'error':str(exc)[:300]})
    finally:
        subprocess.Popen([str(exe)],env=env,close_fds=True)


def single_instance():
    import ctypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.restype=ctypes.c_void_p
    handle=kernel.CreateMutexW(None,False,'Local\\SsakSsakKimchiDownloader.Application')
    if not handle: raise OSError('Cannot establish application instance lock')
    return handle if ctypes.get_last_error()!=183 else None
