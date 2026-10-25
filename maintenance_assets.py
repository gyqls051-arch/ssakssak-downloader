"""Assemble release inputs, preserving pinned corresponding GPL component sources."""
from pathlib import Path
import hashlib
import json
import shutil
import urllib.request
import zipfile
import release_gate

ROOT=Path(__file__).resolve().parent


def main():
    metadata,failures=release_gate.static_failures(unsigned=True)
    if failures: raise RuntimeError('\n'.join(failures))
    folder=ROOT/'release-assets';folder.mkdir(exist_ok=True)
    shutil.copy2(ROOT/'dist'/metadata.canonical_setup_name,folder/metadata.canonical_setup_name)
    for name in ['PRIVACY.md','THIRD-PARTY-NOTICES.md','EULA.txt']:shutil.copy2(ROOT/'docs'/name,folder/name)
    shutil.copy2(ROOT/'LICENSE',folder/'LICENSE')
    with zipfile.ZipFile(folder/'third-party-corresponding-sources.zip','w',zipfile.ZIP_DEFLATED) as bundle:
        for item in json.loads((ROOT/'third_party_sources.json').read_text(encoding='utf-8')):
            with urllib.request.urlopen(item['url'],timeout=90) as response:raw=response.read(40*1024*1024+1)
            if len(raw)>40*1024*1024 or hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Source archive mismatch')
            bundle.writestr(item['name']+('.zip' if item['url'].endswith('.zip') else '.tar.gz'),raw)
        bundle.write(ROOT/'third_party_sources.json','SOURCE-MANIFEST.json')
        for p in (ROOT/'docs/licenses').rglob('*'):
            if p.is_file():bundle.write(p,str(p.relative_to(ROOT/'docs')))


if __name__=='__main__':main()
