"""Update real temporary files and Git histories, without touching a user's client or config."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('update_client', ROOT/'tg.py')
tg = importlib.util.module_from_spec(spec); spec.loader.exec_module(tg)


class ClientUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='telegent_updates_')
        self.root = Path(self.tmp.name)
        self.client = self.root/'tg.py'
        self.old = b"print('old client')\n"
        self.new = b"print('new client')\n"
        self.client.write_bytes(self.old)
        self.release = {'version':'fixture', 'commit':'a'*40, 'files':{
            'tg.py':{'sha256':hashlib.sha256(self.new).hexdigest(),
                     'previous_sha256':[hashlib.sha256(self.old).hexdigest()]}}}

    def tearDown(self): self.tmp.cleanup()

    def test_standalone_update_preserves_identity_and_unrelated_files(self):
        self.release['files']['AGENTS.md']={'sha256':hashlib.sha256(b'new guide').hexdigest(),'previous_sha256':[]}
        (self.root/'config.json').write_text('{"local":"unchanged"}')
        (self.root/'agent.token').write_text('private fixture value')
        with patch.object(tg, 'release_bytes', side_effect=lambda url,limit: self.new if url.endswith('/tg.py') else b'new guide'):
            tg.apply_client_release(self.release, self.client)
        self.assertEqual(self.client.read_bytes().replace(b'\r\n',b'\n'),self.new)
        self.assertEqual((self.root/'AGENTS.md').read_bytes(),b'new guide')
        self.assertEqual((self.root/'agent.token').read_text(),'private fixture value')
        self.assertEqual((self.root/'config.json').read_text(),'{"local":"unchanged"}')

    def test_corrupt_or_interrupted_download_installs_nothing(self):
        self.release['files']={'AGENTS.md':{'sha256':hashlib.sha256(b'guide').hexdigest(),'previous_sha256':[]},**self.release['files']}
        with patch.object(tg,'release_bytes',side_effect=[b'guide',b'corrupt client']):
            with self.assertRaisesRegex(ValueError,'SHA-256'):tg.apply_client_release(self.release,self.client)
        self.assertEqual(self.client.read_bytes(),self.old)
        self.assertFalse((self.root/'AGENTS.md').exists())
        self.assertFalse(list(self.root.glob('.telegent-update-*')))

    def test_local_edits_and_concurrent_edits_are_preserved(self):
        self.client.write_bytes(b'local edit')
        with self.assertRaisesRegex(ValueError,'изменён'):tg.apply_client_release(self.release,self.client)
        self.client.write_bytes(self.old)
        def download(*args):
            self.client.write_bytes(b'edit while downloading');return self.new
        with patch.object(tg,'release_bytes',side_effect=download):
            with self.assertRaisesRegex(ValueError,'во время'):tg.apply_client_release(self.release,self.client)
        self.assertEqual(self.client.read_bytes(),b'edit while downloading')

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.root),*args],stderr=subprocess.STDOUT).decode().strip()

    def history(self):
        self.git('init','-b','main');self.git('config','user.name','alice');self.git('config','user.email','alice@example.test')
        self.git('add','tg.py');self.git('commit','-m','old');old=self.git('rev-parse','HEAD')
        self.client.write_bytes(self.new);self.git('commit','-am','new');new=self.git('rev-parse','HEAD')
        self.git('reset','--hard',old);self.git('remote','add','origin','https://github.com/Takymu/telegent.git')
        self.release['commit']=new
        return old,new

    def test_official_clean_clone_fast_forwards_with_real_git(self):
        old,new=self.history(); real_run=subprocess.run
        (self.root/'local-config.json').write_text('local identity fixture')
        def command(args,**kwargs):
            if 'fetch' in args:return subprocess.CompletedProcess(args,0,'','')
            return real_run(args,**kwargs)
        with patch.object(tg.subprocess,'run',side_effect=command):tg.apply_client_release(self.release,self.client)
        self.assertEqual(self.git('rev-parse','HEAD'),new)
        self.assertEqual(self.client.read_bytes().replace(b'\r\n',b'\n'),self.new)
        self.assertEqual((self.root/'local-config.json').read_text(),'local identity fixture')

    def test_dirty_or_wrong_branch_clone_is_never_fetched(self):
        old,new=self.history();self.client.write_bytes(b'local work')
        with self.assertRaisesRegex(ValueError,'локальные изменения'):tg.apply_client_release(self.release,self.client)
        self.assertEqual(self.git('rev-parse','HEAD'),old)
        self.git('reset','--hard',old);self.git('checkout','-b','feature')
        with self.assertRaisesRegex(ValueError,'рабочей ветке'):tg.apply_client_release(self.release,self.client)

    def test_divergent_clone_is_preserved(self):
        old,new=self.history();self.client.write_bytes(b'local committed work');self.git('commit','-am','local')
        head=self.git('rev-parse','HEAD');real_run=subprocess.run
        def command(args,**kwargs):
            if 'fetch' in args:return subprocess.CompletedProcess(args,0,'','')
            return real_run(args,**kwargs)
        with patch.object(tg.subprocess,'run',side_effect=command):
            with self.assertRaisesRegex(ValueError,'история'):tg.apply_client_release(self.release,self.client)
        self.assertEqual(self.git('rev-parse','HEAD'),head)

    def test_manifest_cannot_supply_paths_or_another_repository(self):
        self.release['files']['../config.json']={'sha256':'b'*64}
        with self.assertRaises(ValueError):tg.validate_client_release({'client':self.release})
        del self.release['files']['../config.json'];self.release['commit']='https://other.example/code'
        with self.assertRaises(ValueError):tg.validate_client_release({'client':self.release})

    def test_failed_background_check_releases_lock_and_reports_local_status(self):
        with patch.object(tg,'update_cache',return_value=str(self.root/'cache')),patch.object(tg,'release_bytes',side_effect=OSError('offline')):
            tg.cmd_update(None,argparse.Namespace(automatic=True))
        self.assertFalse((self.root/'cache/lock').exists())
        self.assertEqual(json.loads((self.root/'cache/status.json').read_text())['status'],'deferred')
        self.assertEqual(self.client.read_bytes(),self.old)

    def test_clean_clone_updates_helper_files_even_when_client_hash_is_unchanged(self):
        self.git('init','-b','main');self.git('config','user.name','alice');self.git('config','user.email','alice@example.test')
        self.git('add','tg.py');self.git('commit','-m','initial');old=self.git('rev-parse','HEAD')
        helper=self.root/'helper.py';helper.write_text('print("helper update")\n')
        self.git('add','helper.py');self.git('commit','-m','helper update');new=self.git('rev-parse','HEAD')
        self.git('reset','--hard',old);self.git('remote','add','origin','https://github.com/Takymu/telegent.git')
        self.release['commit']=new;self.release['files']['tg.py']['sha256']=hashlib.sha256(self.old).hexdigest()
        real_run=subprocess.run
        def command(args,**kwargs):
            if 'fetch' in args:return subprocess.CompletedProcess(args,0,'','')
            return real_run(args,**kwargs)
        with patch.object(tg,'HERE',str(self.root)),patch.object(tg,'__file__',str(self.client)),\
             patch.object(tg,'update_cache',return_value=str(self.root/'cache')),\
             patch.object(tg,'release_bytes',return_value=json.dumps({'client':self.release}).encode()),\
             patch.object(tg.subprocess,'run',side_effect=command):
            tg.cmd_update(None,argparse.Namespace(automatic=True))
        self.assertEqual(self.git('rev-parse','HEAD'),new);self.assertTrue(helper.exists())
        self.assertEqual(self.client.read_bytes(),self.old)

    def test_scheduler_is_background_throttled_and_can_be_disabled(self):
        cfg=self.root/'config.json';cfg.write_text('{}')
        conf=argparse.Namespace(path=str(cfg),url_source=tg.URL_SOURCE)
        env={key:value for key,value in os.environ.items() if key!='TELEGENT_URL'}
        with patch.dict(os.environ,env,clear=True),patch.object(tg,'update_cache',return_value=str(self.root/'cache')),patch.object(tg.subprocess,'Popen') as process:
            tg.schedule_update(conf);tg.schedule_update(conf)
            self.assertEqual(process.call_count,1)
            command=process.call_args.args[0]
            self.assertEqual(command[-2:],['update','--automatic'])
            self.assertIn(str(cfg),command)
            cfg.write_text('{"auto_update":false}')
            (self.root/'cache/scheduled').unlink();tg.schedule_update(conf)
            self.assertEqual(process.call_count,1)

    def test_project_instructions_are_not_overwritten_by_standalone_update(self):
        (self.root/'AGENTS.md').write_text('my project instructions')
        self.release['files']['AGENTS.md']={'sha256':hashlib.sha256(b'new guide').hexdigest(),'previous_sha256':[]}
        with patch.object(tg,'release_bytes',return_value=self.new):tg.apply_client_release(self.release,self.client)
        self.assertEqual((self.root/'AGENTS.md').read_text(),'my project instructions')
        self.assertEqual(self.client.read_bytes(),self.new)

    def test_watch_restarts_after_saving_cursor_and_drops_original_since(self):
        saved={}
        conf=argparse.Namespace(project=None,secretary=False,server='https://chat.example',
                                load_state=lambda:{},save_state=lambda value:saved.update(value))
        args=argparse.Namespace(since=3,all=False,include_own=False,max_burst=30,config=None)
        def restart(executable,arguments):
            self.assertEqual(saved['watch_cursor'],8)
            self.assertNotIn('--since',arguments)
            self.assertNotIn('3',arguments)
            raise StopIteration('restart observed')
        with patch.object(tg,'__file__',str(self.client)),patch.object(tg,'__name__','__main__'),patch.object(tg.sys,'argv',['tg.py','watch','--since','3']),\
             patch.object(tg,'api',side_effect=[{'name':'alice-agent','last_id':3},{'messages':[],'last_id':8}]),\
             patch.object(tg,'schedule_update',side_effect=lambda c:self.client.write_bytes(self.new)),patch.object(tg.os,'execv',side_effect=restart):
            with self.assertRaisesRegex(StopIteration,'restart observed'):tg.cmd_watch(conf,args)


if __name__=='__main__':unittest.main(verbosity=2)
