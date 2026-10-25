"""Disposable Windows runner only: exercise real signed helper old->new transition."""
from pathlib import Path
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
import publisher_update as trust


def wait_for(predicate,timeout,message):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate(): return
        time.sleep(.3)
    raise RuntimeError(message)


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('Installation test requires a disposable GitHub runner')
    root=Path(os.environ['RUNNER_TEMP'])/'ssak-update-transition';root.mkdir()
    assets=Path('release-assets').resolve();raw=(assets/'publisher-manifest.json').read_bytes()
    release=trust.verify_manifest(raw,'1.10');setup=assets/release.name;trust.verify_file(setup,release)
    candidate=root/'candidate';installed=root/'installed'
    args=['/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/AUTORUN=0','/NOICONS','/TASKS=']
    subprocess.run([str(setup),*args,'/DIR='+str(candidate)],check=True,timeout=240)
    exe_name=trust.PRODUCT+'.exe';expected=hashlib.sha256((candidate/exe_name).read_bytes()).hexdigest()
    candidate_health=root/'candidate-health.json'
    subprocess.run([str(candidate/exe_name),'--self-test',str(candidate_health)],check=True,timeout=45)
    candidate_info=json.loads(candidate_health.read_text(encoding='utf-8'))
    if candidate_info.get('version')!=release.version or candidate_info.get('publisher_key')!=trust.PUBLIC_KEY:
        raise RuntimeError('Candidate identity mismatch: '+str(candidate_info))
    print('Installed candidate health:',candidate_info,flush=True)
    old=root/'old-setup.exe'
    url='https://github.com/'+trust.REPOSITORY+'/releases/download/icons-20261024-v1.10/SsakSsakKimchiDownloader-Setup-v1.10.exe'
    with urllib.request.urlopen(url,timeout=60) as response,old.open('wb') as out:shutil.copyfileobj(response,out)
    if hashlib.sha256(old.read_bytes()).hexdigest()!='8e734ccd15ca786054a11eb542afe8749c970db00e0f97006c3981de627c039f':raise ValueError('Bootstrap fixture digest changed')
    subprocess.run([str(old),*args,'/DIR='+str(installed)],check=True,timeout=240)
    if hashlib.sha256((candidate/exe_name).read_bytes()).hexdigest()!=expected:raise RuntimeError('Old installer modified the isolated candidate')
    profile=root/'profile';profile.mkdir();env=os.environ.copy();env['LOCALAPPDATA']=str(profile);env['APPDATA']=str(profile/'Roaming');env['PYINSTALLER_RESET_ENVIRONMENT']='1'
    parent=subprocess.Popen([str(installed/exe_name)],env=env)
    log=profile/trust.PRODUCT/'app.log'
    wait_for(lambda:log.is_file() and 'WebView UI ready' in log.read_text(encoding='utf-8',errors='replace'),60,'Old packaged UI did not load')
    directory=root/'publisher'/'verified-test';directory.mkdir(parents=True)
    shutil.copy2(setup,directory/release.name);helper=directory/'update-helper.exe';shutil.copy2(candidate/exe_name,helper)
    ticket=directory/'ticket.json';trust.atomic_json(ticket,{'manifest':json.loads(raw),'current':'1.10','install':str(installed),'parent':parent.pid})
    helper_error=root/'helper-stderr.log'
    with helper_error.open('wb') as error:
        process=subprocess.Popen([str(helper),'--apply-publisher-update',str(ticket)],env=env,stdout=error,stderr=error)
        try:
            wait_for(lambda:(directory/'ready').is_file(),35,'Helper ownership/ready check failed')
        except Exception:
            print('Helper pid/exit:',process.pid,process.poll(),flush=True)
            print('Helper stderr:',helper_error.read_text(encoding='utf-8',errors='replace'),flush=True)
            print('Application log:',log.read_text(encoding='utf-8',errors='replace'),flush=True)
            raise
    # Close only this test installation's owned windows, after helper acknowledgement.
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    callback=ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)
    def close_owned(hwnd,unused):
        pid=ctypes.c_ulong();ctypes.windll.user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        handle=kernel.OpenProcess(0x1000,False,pid.value)
        if handle:
            try:
                size=ctypes.c_ulong(32768);buf=ctypes.create_unicode_buffer(size.value)
                if kernel.QueryFullProcessImageNameW(handle,0,buf,ctypes.byref(size)) and Path(buf.value).resolve()==(installed/exe_name).resolve():
                    ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd),0x10,0,0)
            finally:kernel.CloseHandle(handle)
        return True
    ctypes.windll.user32.EnumWindows(callback(close_owned),0)
    parent.wait(timeout=35);process.wait(timeout=300)
    state=json.loads((root/'publisher/state.json').read_text(encoding='utf-8'))
    if state.get('installed')!=release.version:raise RuntimeError('Transition/rollback result: '+str(state))
    if hashlib.sha256((installed/exe_name).read_bytes()).hexdigest()!=expected:raise ValueError('Installed app does not match tested candidate')
    wait_for(lambda:log.is_file() and ('v'+release.version) in log.read_text(encoding='utf-8',errors='replace'),60,'New app did not relaunch')
    print('REAL TRANSITION PASS: pinned old Setup -> signed helper -> verified new Setup -> startup health check -> relaunch',flush=True)


if __name__=='__main__':main()
