"""Explicit manual unsigned release. No runtime verification is disabled."""
from pathlib import Path
import sys,subprocess,os,json,shutil,importlib.metadata as md,re
import release_gate as gate
ROOT=Path(__file__).resolve().parent

def main():
 if sys.platform!="win32" or sys.version_info[:2]!=(3,13):raise RuntimeError("Use the locked Windows Python 3.13 build environment")
 metadata,failures=gate.static_failures(unsigned=True)
 if failures:raise RuntimeError("\n".join(failures))
 entries=[]
 for entry in gate._logical_lock_entries(gate._read(gate.RELEASE_LOCK)):
  if entry.startswith("--index-url"):continue
  match=re.match(r"^([A-Za-z0-9_.-]+)==([^\s]+)",entry)
  name,version=match.groups()
  if md.version(name)!=version:raise RuntimeError("Installed dependency differs from lock: "+name)
  entries.append({"name":name,"version":version})
 inventory=ROOT/"build/release-metadata";inventory.mkdir(parents=True,exist_ok=True)
 (inventory/"python-packages.json").write_text(json.dumps(entries,indent=2)+"\n",encoding="utf-8")
 for file in [gate.RELEASE_LOCK,gate.BUNDLE_LOCK]:shutil.copy2(file,inventory/file.name)
 bundle=ROOT/"potprovider"
 for required in ["node.exe","NODE-LICENSE.txt","server/build/main.js","server/package-lock.json","plugin/yt_dlp_plugins"]:
  if not (bundle/required).exists():raise RuntimeError("Pinned bundle missing: "+required)
 # Include runtime allowlist only: never ship build caches or source-control state.
 runtime=ROOT/"build/manual-runtime-bundle";runtime.mkdir(parents=True,exist_ok=True)
 for file in ["node.exe","NODE-LICENSE.txt"]:shutil.copy2(bundle/file,runtime/file)
 for folder in ["server","plugin"]:
  shutil.copytree(bundle/folder,runtime/folder,dirs_exist_ok=True,ignore=shutil.ignore_patterns(".git","__pycache__","*.pyc"))
 args=[sys.executable,"-m","PyInstaller","--noconfirm","--onefile","--windowed","--name","SsakSsakKimchiDownloader",
       "--icon",str(ROOT/"app.ico"),"--version-file",str(ROOT/"version_info.txt"),
       "--distpath",str(ROOT/"build/app"),"--workpath",str(ROOT/"build/pyinstaller-manual"),"--specpath",str(ROOT/"build/spec")]
 for src,dest in [("web_ui","web_ui"),("app.ico","."),("APP-ICON-LICENSE.txt",".")]:args.extend(["--add-data",str(ROOT/src)+";"+dest])
 for package in ["webview","pythonnet","clr_loader"]:args.extend(["--collect-all",package])
 args.extend(["--hidden-import","clr","--collect-submodules","mutagen","--exclude-module","playwright","--exclude-module","curl_cffi",str(ROOT/"main.py")])
 subprocess.run(args,cwd=ROOT,check=True)
 iscc=Path(os.environ["LOCALAPPDATA"])/"Programs/Inno Setup 6/ISCC.exe"
 if not iscc.is_file():
  iscc=Path(os.environ.get("ProgramFiles(x86)","C:/Program Files (x86)"))/"Inno Setup 6/ISCC.exe"
 if not iscc.is_file():raise RuntimeError("Inno Setup 6 is required")
 subprocess.run([str(iscc),"/Q","/DMyAppVersion="+metadata.app_version,"/DMyFileVersion="+metadata.file_version,
                 "/DPotBundleDir="+str(runtime),str(ROOT/"installer-ci.iss")],cwd=ROOT,check=True)
 output=ROOT/"dist"/metadata.canonical_setup_name
 if not output.is_file():raise RuntimeError("Canonical manual Setup was not produced")
 print("MANUAL UNSIGNED SETUP:",output,flush=True)
 return 0
if __name__=="__main__":raise SystemExit(main())
