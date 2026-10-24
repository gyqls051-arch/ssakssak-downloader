import release_gate as g

def test_unsigned_manual_checks_pass_and_signed_checks_stay_closed():
 metadata,failures=g.static_failures(unsigned=True)
 assert metadata is not None and not failures, failures
 _,signed=g.static_failures()
 assert any("TRUSTED_SIGNER_THUMBPRINTS" in f for f in signed)

def test_unsigned_still_rejects_mutable_dependency(monkeypatch):
 original=g._read
 def read(path):
  text=original(path)
  return text.replace("yt-dlp==", "yt-dlp>=") if path==g.RELEASE_LOCK else text
 monkeypatch.setattr(g,"_read",read)
 _,failures=g.static_failures(unsigned=True)
 assert any("not exactly pinned" in f for f in failures)

def test_unsigned_still_rejects_missing_ffmpeg_pin(monkeypatch):
 import re
 original=g._read
 def read(path):
  text=original(path)
  return re.sub(r'^FFMPEG_EXECUTABLE_SHA256 = .*$', 'FFMPEG_EXECUTABLE_SHA256 = ""',text,flags=re.M) if path.name=="main.py" else text
 monkeypatch.setattr(g,"_read",read)
 _,failures=g.static_failures(unsigned=True)
 assert any("EXE SHA-256 pin" in f for f in failures)

def test_unsigned_still_rejects_unresolved_publication_text(monkeypatch):
 original=g._read
 monkeypatch.setattr(g,"_read",lambda p:original(p)+("\nRELEASE_BLOCKER_TEST" if p.name=="PRIVACY.md" else ""))
 _,failures=g.static_failures(unsigned=True)
 assert any("unresolved legal/release placeholder" in f for f in failures)
