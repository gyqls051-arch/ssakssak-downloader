"""Trusted CI control plane. No upstream package code runs in the signing job."""
from pathlib import Path
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parent
REPO='gyqls051-arch/ssakssak-downloader'
SOURCE_BRANCH='codex/family-freeware-hardening-20261023'


def api(url):
    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'SsakSsak-Maintenance'}),timeout=30) as response:
        raw=response.read(10*1024*1024+1)
    if len(raw)>10*1024*1024: raise ValueError('Oversized metadata')
    return json.loads(raw)


def run(*args,**kwargs):
    return subprocess.run(list(args),check=True,cwd=ROOT,**kwargs)


def prepare(force=False):
    old=re.search(r'^APP_VERSION = "([^"]+)"',(ROOT/'main.py').read_text(encoding='utf-8'),re.M).group(1)
    lock=ROOT/'requirements-release-win.lock';text=lock.read_text(encoding='utf-8')
    previous=re.search(r'^yt-dlp==([^ ]+)',text,re.M).group(1)
    info=api('https://pypi.org/pypi/yt-dlp/json');latest=info['info']['version']
    if not re.fullmatch(r'\d{4}\.\d{1,2}\.\d{1,2}',latest): raise ValueError('Only dated stable releases are eligible')
    changed=tuple(map(int,latest.split('.')))>tuple(map(int,previous.split('.')))
    if changed:
        candidates=[f for f in info['urls'] if f['filename'].endswith('-py3-none-any.whl') and not f.get('yanked')]
        if len(candidates)!=1: raise ValueError('Expected one official pure Python engine wheel')
        wheel=candidates[0]
        if not wheel['url'].startswith('https://files.pythonhosted.org/') or not re.fullmatch('[a-f0-9]{64}',wheel['digests']['sha256']): raise ValueError('Invalid engine origin')
        lock.write_text(re.sub(r'^yt-dlp==.*$',f"yt-dlp=={latest} --hash=sha256:{wheel['digests']['sha256']}",text,flags=re.M),encoding='utf-8')
        # An upstream dependency/license contract change requires intervention.
        details=info['info']
        if details.get('requires_dist'):
            mandatory=[x for x in details['requires_dist'] if 'extra ==' not in x and 'extra==' not in x]
            if mandatory: raise ValueError('New mandatory engine dependencies require review: '+str(mandatory))
        run('git','add','requirements-release-win.lock')
    released=subprocess.run(['gh','release','view','v'+old,'--repo',REPO,'--json','tagName'],capture_output=True,text=True)
    if released.returncode and 'release not found' not in released.stderr.lower():
        raise RuntimeError('Cannot determine release state: '+released.stderr)
    missing=released.returncode!=0
    candidate=changed or force or missing
    if candidate and not missing:
        parts=[int(x) for x in old.split('.')];parts += [0]*(3-len(parts));parts[2]+=1
        new='.'.join(map(str,parts));four=new+'.0'
        for filename in ['main.py','version_info.txt','README.md','web/index.html']:
            p=ROOT/filename;s=p.read_text(encoding='utf-8')
            if filename=='version_info.txt':
                before=old+'.0' if len(old.split('.'))==3 else old+'.0.0'
                s=s.replace(before,four)
                s=re.sub(r'(filevers|prodvers)=\([^)]+\)',lambda m:m.group(1)+'=('+', '.join(map(str,parts+[0]))+')',s)
            else:s=s.replace('APP_VERSION = "'+old+'"','APP_VERSION = "'+new+'"').replace('Setup-v'+old+'.exe','Setup-v'+new+'.exe').replace('/v'+old+'/','/v'+new+'/')
            p.write_text(s,encoding='utf-8')
        channel=ROOT/'version.json';data=json.loads(channel.read_text(encoding='utf-8-sig'));data['latest_version']=new
        channel.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        old=new
    if changed:
        p=ROOT/'docs/THIRD-PARTY-NOTICES.md';s=p.read_text(encoding='utf-8');s=re.sub(r'(\| yt-dlp \| )[^|]+',r'\g<1>'+latest+' ',s);p.write_text(s,encoding='utf-8')
    dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True))
    if dirty:
        run('git','config','user.name','github-actions[bot]');run('git','config','user.email','41898282+github-actions[bot]@users.noreply.github.com')
        run('git','add','--','requirements-release-win.lock','main.py','version_info.txt','version.json','README.md','web/index.html','docs/THIRD-PARTY-NOTICES.md')
        run('git','commit','-m','chore: validated engine candidate '+latest)
        run('git','push','origin','HEAD:'+SOURCE_BRANCH)
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as out:out.write(f'build={str(candidate).lower()}\nsha={sha}\nversion={old}\n')
    print(json.dumps({'build':candidate,'sha':sha,'version':old,'engine':latest}))


def smoke(source):
    import main
    fixture=ROOT/'tests/fixtures/update-smoke.mp4'
    url=f'https://raw.githubusercontent.com/{REPO}/{source}/tests/fixtures/update-smoke.mp4'
    destination=ROOT/'build/network-smoke';destination.mkdir(parents=True,exist_ok=True)
    outcomes=[]
    config=main.DownloadConfig(url=url,out_dir=str(destination),format_str='best',is_audio_only=False,
                               container='mp4',ffmpeg_dir=str(ROOT/'build/ffmpeg/bin'),cookies_browser=None,
                               embed_metadata=False,use_playwright=False,target_height=None)
    ffmpeg=main.FfmpegInstaller();asset=ffmpeg._resolve_asset()
    # Use the exact pinned FFmpeg; CI has no user cache or executable fallbacks.
    archive=ROOT/'build/ffmpeg-pinned.zip';archive.parent.mkdir(parents=True,exist_ok=True)
    ffmpeg._download(asset,archive,lambda p:None);ffmpeg._verify_zip(archive)
    binary=Path(config.ffmpeg_dir)/'ffmpeg.exe';binary.parent.mkdir(parents=True,exist_ok=True)
    ffmpeg._extract(archive,binary);ffmpeg._verify_executable(binary,main.FFMPEG_EXECUTABLE_SHA256,main.FFMPEG_EXECUTABLE_SIZE)
    assert ffmpeg._smoke_test(binary)
    files=[]
    worker=main.Downloader(on_progress=lambda p:None,on_done=lambda ok,error:outcomes.append((ok,error)),
                           on_item_done=lambda ident,path=None,dims=None:files.append(path),on_status=lambda message:None)
    worker.run(config)
    if not outcomes or not outcomes[-1][0] or not files: raise RuntimeError('Consumer download smoke failed: '+str(outcomes))
    media=Path(files[-1]);run(str(binary),'-v','error','-i',str(media),'-f','null','-')
    print('Actual guarded network download -> consumer output -> FFmpeg decode PASS',media.stat().st_size)


def sign():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import publisher_update as trust
    folder=ROOT/'release-assets';files=list(folder.glob('*Setup-v*.exe'))
    if len(files)!=1: raise ValueError('One canonical Setup is required')
    setup=files[0];version=re.fullmatch(r'SsakSsakKimchiDownloader-Setup-v([0-9.]+)\.exe',setup.name).group(1)
    trust.version_tuple(version)
    source=os.environ['CANDIDATE_SHA']
    body={'schema':1,'product':trust.PRODUCT,'version':version,'issued':int(time.time()),
          'expires':int(time.time())+7*86400,'size':setup.stat().st_size,
          'sha256':hashlib.sha256(setup.read_bytes()).hexdigest(),'source':source}
    key=Ed25519PrivateKey.from_private_bytes(base64.b64decode(os.environ['PUBLISHER_UPDATE_KEY'],validate=True))
    if key.public_key().public_bytes_raw().hex()!=trust.PUBLIC_KEY: raise ValueError('Signing key does not match bundled trust root')
    manifest=trust.sign_manifest(body,key);trust.verify_file(setup,trust.verify_manifest(manifest,'0.0'))
    (folder/'publisher-manifest.json').write_bytes(manifest)
    # The old homepage filename is an installer alias, never a raw app executable.
    (folder/'SsakSsakKimchiDownloader.exe').write_bytes(setup.read_bytes())
    (folder/'SHA256SUMS.txt').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in sorted(folder.iterdir()) if p.name!='SHA256SUMS.txt'),encoding='utf-8')
    print('Signed tested candidate',version,source)


def publish():
    import publisher_update as trust
    folder=ROOT/'release-assets'
    manifest=(folder/'publisher-manifest.json').read_bytes()
    release=trust.verify_manifest(manifest,'0.0')
    setup=folder/release.name;trust.verify_file(setup,release)
    version=release.version
    source=json.loads(manifest)['payload']['source']
    tag='v'+version
    releases=json.loads(subprocess.check_output(['gh','api',f'repos/{REPO}/releases?per_page=100&_={time.time_ns()}'],text=True))
    current=next((r for r in releases if r['tag_name']==tag),None)
    if current is None:
        request=ROOT/'release-request.json'
        request.write_text(json.dumps({'tag_name':tag,'target_commitish':source,'draft':True,
            'name':'싹싹김치 다운로더 '+version,
            'body':'무료 Windows 설치판. 앱 내 고정 공개키로 업데이트 전자서명을 검증합니다. Windows Authenticode 인증서는 사용하지 않습니다. 실제 다운로드·설치 전환 검사 통과. Source: '+source},ensure_ascii=False),encoding='utf-8')
        current=json.loads(subprocess.check_output(['gh','api','-X','POST',f'repos/{REPO}/releases','--input',str(request)],text=True))
    if current['target_commitish']!=source:raise ValueError('Existing release belongs to a different candidate')
    # Keep the exact creation response ID. A newly created draft may not appear in lists yet.
    release_id=current['id']
    for p in folder.iterdir():
        existing=next((a for a in current['assets'] if a['name']==p.name),None)
        if existing:
            if existing.get('digest')!='sha256:'+hashlib.sha256(p.read_bytes()).hexdigest(): raise ValueError('Existing release differs')
        else:
            from urllib.parse import quote
            upload=f'https://uploads.github.com/repos/{REPO}/releases/{release_id}/assets?name={quote(p.name)}'
            request=urllib.request.Request(upload,data=p.read_bytes(),method='POST',headers={
                'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Content-Type':'application/octet-stream',
                'Accept':'application/vnd.github+json','User-Agent':'SsakSsak-Maintenance'})
            with urllib.request.urlopen(request,timeout=180) as response:uploaded=json.load(response)
            if uploaded.get('digest')!='sha256:'+hashlib.sha256(p.read_bytes()).hexdigest():raise ValueError('Uploaded asset digest differs')
    remote=json.loads(subprocess.check_output(['gh','api',f'repos/{REPO}/releases/{release_id}?_={time.time_ns()}'],text=True))
    for p in folder.iterdir():
        asset=next(a for a in remote['assets'] if a['name']==p.name)
        if asset.get('digest')!='sha256:'+hashlib.sha256(p.read_bytes()).hexdigest() or asset['size']!=p.stat().st_size: raise ValueError('Remote asset mismatch')
    run('gh','api','-X','PATCH',f'repos/{REPO}/releases/{release_id}','-F','draft=false','-f','make_latest=true')
    update_channel(manifest,version)


def resume_publish(run_id):
    if not re.fullmatch('[0-9]+',run_id):raise ValueError('Invalid validated run ID')
    jobs=json.loads(subprocess.check_output(['gh','api',f'repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100'],text=True))['jobs']
    for name in ['build','sign','transition']:
        if not any(j['name']==name and j['conclusion']=='success' for j in jobs):raise ValueError('Run did not pass '+name)
    manifest=json.loads((ROOT/'release-assets/publisher-manifest.json').read_bytes())
    changed=subprocess.check_output(['git','diff','--name-only',manifest['payload']['source'],'HEAD'],text=True,cwd=ROOT).splitlines()
    allowed={'maintenance.py','test_maintenance.py','.github/workflows/maintenance.yml'}
    if set(changed)-allowed:raise ValueError('Application changed after validation; rebuild required')
    publish()


def update_channel(manifest,version):
    name='updates/stable.json'
    result=subprocess.run(['gh','api',f'repos/{REPO}/contents/{name}?ref=main'],capture_output=True,text=True)
    existing=json.loads(result.stdout) if result.returncode==0 else {}
    request={'message':'chore: trusted update channel '+version,'branch':'main','content':base64.b64encode(manifest).decode()}
    if existing.get('sha'):request['sha']=existing['sha']
    payload=ROOT/'channel-request.json';payload.write_text(json.dumps(request),encoding='utf-8')
    run('gh','api','-X','PUT',f'repos/{REPO}/contents/{name}','--input',str(payload))
    # Bootstrap clients still use the legacy notification-only channel once.
    result=subprocess.run(['gh','api',f'repos/{REPO}/contents/version.json?ref=main'],capture_output=True,text=True,check=True)
    legacy=json.loads(result.stdout)
    data=json.loads(base64.b64decode(legacy['content']))
    data['latest_version']=version
    data['download_url']=f'https://github.com/{REPO}/releases/latest'
    data['release_notes']='전자서명 기반 자동 업데이트 지원. 이 전환 버전은 한 번 직접 설치해 주세요.'
    request={'message':'chore: transition update notice '+version,'branch':'main','sha':legacy['sha'],
             'content':base64.b64encode(json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8')).decode()}
    payload.write_text(json.dumps(request),encoding='utf-8')
    run('gh','api','-X','PUT',f'repos/{REPO}/contents/version.json','--input',str(payload))


def refresh():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import publisher_update as trust
    data=api(trust.CHANNEL_URL);body=data['payload']
    # Authenticate the previous channel even if its TTL has elapsed.
    trust.verify_manifest(trust.canonical(data),'0.0',now=body['issued'])
    latest=json.loads(subprocess.check_output(['gh','api',f'repos/{REPO}/releases/latest'],text=True))
    if latest['tag_name']!='v'+body['version']:raise ValueError('Stale channel; refusing to re-sign')
    asset=next((a for a in latest['assets'] if a['name']==f"{trust.PRODUCT}-Setup-v{body['version']}.exe"),None)
    if not asset or asset['size']!=body['size'] or asset.get('digest')!='sha256:'+body['sha256']:raise ValueError('Channel/release bytes differ')
    key=Ed25519PrivateKey.from_private_bytes(base64.b64decode(os.environ['PUBLISHER_UPDATE_KEY'],validate=True))
    if key.public_key().public_bytes_raw().hex()!=trust.PUBLIC_KEY: raise ValueError('Wrong renewal key')
    body['issued']=int(time.time());body['expires']=body['issued']+7*86400
    update_channel(trust.sign_manifest(body,key),body['version'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','smoke','sign','publish','refresh','resume']);p.add_argument('--force',action='store_true');p.add_argument('--source');p.add_argument('--run')
    args=p.parse_args()
    if args.action=='prepare':prepare(args.force)
    elif args.action=='smoke':smoke(args.source)
    elif args.action=='sign':sign()
    elif args.action=='publish':publish()
    elif args.action=='refresh':refresh()
    elif args.action=='resume':resume_publish(args.run)
