import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import maintenance as m
import publisher_update as trust


class PublishRecoveryTests(unittest.TestCase):
    def exercise(self, existing):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);folder=root/'release-assets';folder.mkdir()
            setup=folder/'SsakSsakKimchiDownloader-Setup-v1.11.0.exe';setup.write_bytes(b'tested bytes')
            manifest={'payload':{'source':'a'*40}}
            (folder/'publisher-manifest.json').write_text(json.dumps(manifest))
            release=SimpleNamespace(name=setup.name,version='1.11.0')
            record={'id':423150362,'tag_name':'v1.11.0','target_commitish':'a'*40,'draft':True,
                    'assets':[{'name':p.name,'size':p.stat().st_size,'digest':'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest()} for p in folder.iterdir()]}
            calls=[]
            def api(args,**kwargs):
                calls.append(args)
                if any('/releases?' in a for a in args):return json.dumps([record] if existing else []).encode()
                return json.dumps(record).encode()
            with patch.object(m,'ROOT',root),patch.object(m.subprocess,'check_output',side_effect=api),patch.object(m,'run') as mutate,patch.object(m,'update_channel') as channel,patch.object(trust,'verify_manifest',return_value=release),patch.object(trust,'verify_file'):
                m.publish()
            posts=[c for c in calls if 'POST' in c]
            self.assertEqual(len(posts),0 if existing else 1)
            self.assertEqual(mutate.call_args.args[0:5],('gh','api','-X','PATCH','repos/'+m.REPO+'/releases/423150362'))
            channel.assert_called_once()

    def test_existing_draft_reused_without_duplicate_release(self):self.exercise(True)

    def test_new_draft_creation_response_id_used_without_list_visibility(self):self.exercise(False)

    def test_resume_rejects_a_run_without_successful_real_transition(self):
        with patch.object(m.subprocess,'check_output',return_value=json.dumps({'jobs':[{'name':'build','conclusion':'success'},{'name':'sign','conclusion':'success'},{'name':'transition','conclusion':'failure'}]})),patch.object(m,'publish') as publish:
            with self.assertRaises(ValueError):m.resume_publish('40162322536')
            publish.assert_not_called()


if __name__=='__main__':unittest.main()
