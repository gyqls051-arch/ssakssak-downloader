import base64
import copy
import hashlib
import json
import time
import unittest
from unittest.mock import patch, Mock
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import publisher_update as u


class TrustTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes_raw().hex()
        self.now = int(time.time())
        self.body = {'schema': 1, 'product': u.PRODUCT, 'version': '1.11.1',
                     'issued': self.now, 'expires': self.now + 86400,
                     'size': 6000000, 'sha256': 'ab' * 32,
                     'source': 'a' * 40}

    def signed(self, body=None):
        body = self.body if body is None else body
        return u.sign_manifest(body, self.key)

    def verify(self, raw):
        return u.verify_manifest(raw, '1.11.0', public_key=self.public, now=self.now)

    def test_signature_and_canonical_asset(self):
        asset = self.verify(self.signed())
        self.assertEqual(asset.version, '1.11.1')
        self.assertEqual(asset.url, 'https://github.com/' + u.REPOSITORY + '/releases/download/v1.11.1/SsakSsakKimchiDownloader-Setup-v1.11.1.exe')

    def test_tampered_payload_rejected(self):
        envelope = json.loads(self.signed())
        envelope['payload']['sha256'] = 'cd' * 32
        with self.assertRaises(ValueError): self.verify(json.dumps(envelope).encode())

    def test_wrong_key_rejected(self):
        with self.assertRaises(ValueError):
            self.verify(u.sign_manifest(self.body, Ed25519PrivateKey.generate()))

    def test_expired_future_wrong_product_and_invalid_fields(self):
        for field, value in [('expires', self.now - 1), ('issued', self.now + 3600),
                             ('product', 'different-app'), ('schema', 2),
                             ('size', True), ('size', 999999999), ('sha256', 'bad'),
                             ('version', '../evil'), ('source', 'not-a-commit')]:
            with self.subTest(field=field, value=value):
                body = dict(self.body, **{field: value})
                with self.assertRaises(ValueError): self.verify(self.signed(body))

    def test_replay_and_current_version_rejected(self):
        for version in ['1.10', '1.11.0']:
            with self.assertRaises(ValueError): self.verify(self.signed(dict(self.body, version=version)))
        with self.assertRaises(ValueError):
            u.verify_manifest(self.signed(), '1.11.0', minimum='1.11.2', public_key=self.public)

    def test_duplicate_keys_rejected(self):
        raw = self.signed().replace(b'"schema":1', b'"schema":1,"schema":1')
        with self.assertRaises(ValueError): self.verify(raw)

    def test_payload_digest_size_and_pe_mismatch_rejected(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp)/'setup.exe'
            data = b'MZ' + b'0' * (6000000 - 2)
            p.write_bytes(data)
            body = dict(self.body, sha256=hashlib.sha256(data).hexdigest())
            asset = self.verify(self.signed(body))
            u.verify_file(p, asset)
            p.write_bytes(b'MZevil')
            with self.assertRaises(ValueError): u.verify_file(p, asset)
            p.unlink(); p.symlink_to(Path(tmp)/'missing')
            with self.assertRaises((ValueError, OSError)): u.verify_file(p, asset)

    def test_download_cancellation_and_oversize_preserve_old_application(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);old=root/'old.exe';old.write_bytes(b'old version')
            asset=self.verify(self.signed())
            service=u.PublisherUpdater('1.11.0',root/'cache',lambda url:io.BytesIO(b'MZ'+b'x'*asset.size),public_key=self.public)
            for cancelled in [lambda:True,lambda:False]:
                with self.assertRaises(ValueError):service.download(asset,cancelled)
            self.assertEqual(old.read_bytes(),b'old version')
            self.assertEqual(list((root/'cache').iterdir()),[])

    def test_installer_failure_restores_old_files_and_relaunches_old_app(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);installed=root/'installed';installed.mkdir()
            exe=installed/(u.PRODUCT+'.exe');exe.write_bytes(b'old application')
            directory=root/'publisher/verified-test';directory.mkdir(parents=True)
            asset=self.verify(self.signed());setup=directory/asset.name;setup.write_bytes(b'MZ')
            ticket=directory/'ticket.json';u.atomic_json(ticket,{'manifest':json.loads(asset.raw),'current':'1.11.0','parent':123,'install':str(installed)})
            kernel=Mock();kernel.OpenProcess.return_value=1;kernel.WaitForSingleObject.return_value=0
            def image(handle,unused,buffer,size):buffer.value=str(exe);return 1
            kernel.QueryFullProcessImageNameW.side_effect=image
            def fail_install(*args,**kwargs):exe.write_bytes(b'broken new application');return Mock(returncode=5)
            with patch('ctypes.WinDLL',return_value=kernel),patch.object(u,'verify_manifest',return_value=asset),patch.object(u,'verify_file'),patch.object(u,'deny_write_lock',lambda path:__import__('contextlib').nullcontext()),patch.object(u.subprocess,'run',side_effect=fail_install),patch.object(u.subprocess,'Popen') as launch:
                u.apply_ticket(ticket)
            self.assertEqual(exe.read_bytes(),b'old application')
            state=json.loads((root/'publisher/state.json').read_text())
            self.assertEqual(state['failed'],'1.11.1')
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[0],[str(exe)])

    def test_windowed_startup_probe_writes_health_without_stdout(self):
        import main
        with TemporaryDirectory() as tmp:
            destination=Path(tmp)/'health.json'
            with patch.object(main.sys,'argv',['SsakSsakKimchiDownloader.exe','--self-test',str(destination)]),patch.object(main.sys,'stdout',None),patch.object(main,'potprovider_dir',return_value=Path(tmp)),patch.object(main,'_ui_html_path',return_value=__file__):
                main.main()
            self.assertEqual(json.loads(destination.read_text())['version'],main.APP_VERSION)

    def test_prune_only_authenticated_owned_caches(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            for number in range(4):
                folder=root/('verified-'+str(number));folder.mkdir()
                (folder/'manifest.json').write_bytes(self.signed())
                (folder/'old-backup.exe').write_bytes(b'old')
            unrelated=root/'verified-unrelated';unrelated.mkdir();(unrelated/'keep').write_text('user')
            service=u.PublisherUpdater('1.11.0',root,None,public_key=self.public)
            service.prune_cache()
            self.assertEqual(len(list(root.glob('verified-*'))),3)
            self.assertEqual((unrelated/'keep').read_text(),'user')

    def test_api_update_reserves_both_download_and_analysis_admission(self):
        import main
        api=main.Api()
        api._updating=True
        with patch.object(main,'validate_public_http_url'):
            self.assertFalse(api.analyze('https://example.com/a.mp4')['ok'])
            self.assertFalse(api.start_download('test_job','https://example.com/a.mp4',main.QUALITY_TABLE[0][0],main.FORMAT_TABLE[0][0])['ok'])
        api._shutdown()

    def test_updater_redirect_rejected_before_connecting_to_untrusted_host(self):
        import main
        import urllib.request
        handler=main._PublisherRedirectHandler()
        req=urllib.request.Request('https://github.com/'+u.REPOSITORY+'/releases/latest')
        for url in ['http://github.com/insecure','https://127.0.0.1/private','https://attacker.example/file.exe']:
            with self.subTest(url=url),patch.object(main,'validate_public_http_url') as public:
                with self.assertRaises(RuntimeError):handler.redirect_request(req,None,302,'redirect',{},url)
                public.assert_not_called()


if __name__ == '__main__': unittest.main()
