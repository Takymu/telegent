"""Client labels are self-reported presentation metadata, never authority."""
import importlib.util
import json
import os
import sqlite3
import subprocess
import unittest
from unittest import mock

import test_attachments as fixture


class AgentClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass()
        cls.fx = fixture.AttachmentTest()

    @classmethod
    def tearDownClass(cls):
        fixture.AttachmentTest.tearDownClass()

    def setUp(self):
        with self.fx.s.DB_LOCK:
            self.fx.s.db().execute('UPDATE users SET agent_client=NULL')
        config = json.loads(self.fx.config.read_text())
        config.pop('client', None); config['auto_update'] = False
        self.fx.config.write_text(json.dumps(config))
        for key in ('CODEX_THREAD_ID','CODEX_SESSION_ID','CLAUDECODE'):
            self.fx.env.pop(key, None)

    def test_only_authenticated_agent_updates_its_display_hint(self):
        for client in ('codex','claude'):
            me = self.fx.api('GET','/api/me',headers={'X-Telegent-Client':client})
            self.assertEqual(me['agent_client'],client)
            self.assertEqual(me['kind'],'agent'); self.assertFalse(me['is_admin'])
            other = next(u for u in me['users'] if u['id']=='alice-agent')
            self.assertIsNone(other['agent_client'])
            self.assertEqual(me['project']['role'],'agent')
        self.assertEqual(self.fx.api('GET','/api/me',headers={'X-Telegent-Client':'human'})['agent_client'],'claude')
        human = self.fx.api('GET','/api/me',who='alice',headers={'X-Telegent-Client':'codex'})
        self.assertIsNone(human['agent_client']); self.assertEqual(human['kind'],'human')

    def test_message_author_client_uses_stable_identity_and_keeps_scope(self):
        mid = self.fx.api('POST','/api/messages',{'body':'caption'},headers={'X-Telegent-Client':'claude'})['id']
        message = self.fx.api('GET',f'/api/messages/{mid}',who='alice')['message']
        self.assertEqual(message['author_client'],'claude')
        self.fx.api('POST','/api/rename',{'account_id':'bob-agent','new':'renamed-agent'},who='alice')
        message = self.fx.api('GET',f'/api/messages/{mid}',who='alice')['message']
        self.assertEqual(message['author_id'],'bob-agent'); self.assertEqual(message['author_client'],'claude')
        self.fx.error(404,'GET',f'/api/messages/{mid}',who='carol')
        self.assertNotIn('bob-agent',{u['id'] for u in self.fx.api('GET','/api/me',who='carol')['users']})

    def test_cli_saved_client_environment_and_hook_report_without_changing_config(self):
        for client in ('codex','claude'):
            config = json.loads(self.fx.config.read_text()); config['client'] = client
            self.fx.config.write_text(json.dumps(config)); before = self.fx.config.read_bytes()
            result = self.fx.cli('whoami')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(self.fx.api('GET','/api/me')['agent_client'],client)
            self.assertEqual(self.fx.config.read_bytes(),before)
        self.fx.cli('hook')
        self.assertEqual(self.fx.api('GET','/api/me')['agent_client'],'claude')
        self.fx.api('GET','/api/summary?metadata=1&client=codex')
        self.assertEqual(self.fx.api('GET','/api/me')['agent_client'],'codex')

    def test_legacy_claude_hook_without_upgrade_or_client_header(self):
        legacy=self.fx.work/'legacy-tg.py'
        source=subprocess.check_output(['git','show','82754b1:tg.py'],cwd=fixture.ROOT)
        self.assertNotIn(b'X-Telegent-Client',source)
        legacy.write_bytes(source)
        config_before=self.fx.config.read_bytes()
        result=subprocess.run([os.sys.executable,'-X','utf8',str(legacy),'--config',str(self.fx.config),'hook'],
                              env=self.fx.env,capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.fx.api('GET','/api/me')['agent_client'],'claude')
        self.assertEqual(legacy.read_bytes(),source)
        self.assertEqual(self.fx.config.read_bytes(),config_before)

    def test_legacy_detection_does_not_guess_from_watch_name_or_human_requests(self):
        headers={'User-Agent':'telegent-tg/1.0','X-Telegent-Host':'fixture/win'}
        self.fx.api('GET','/api/summary?metadata=1')
        self.assertIsNone(self.fx.api('GET','/api/me')['agent_client'])
        self.fx.api('GET','/api/wait?timeout=0.01&after=999999&full=0&for_me=1',headers=headers)
        self.assertIsNone(self.fx.api('GET','/api/me')['agent_client'])
        self.fx.api('GET','/api/summary?metadata=1&limit=10',who='alice',headers=headers)
        self.assertIsNone(self.fx.api('GET','/api/me',who='alice')['agent_client'])
        self.fx.api('GET','/api/me',headers={'X-Telegent-Client':'codex'})
        self.fx.api('GET','/api/summary?metadata=1&limit=10',headers=headers)
        self.assertEqual(self.fx.api('GET','/api/me')['agent_client'],'codex')

    def test_unknown_or_ambiguous_environment_stays_unknown(self):
        spec=importlib.util.spec_from_file_location('avatar_client',fixture.ROOT/'tg.py')
        tg=importlib.util.module_from_spec(spec);spec.loader.exec_module(tg)
        for env,expected in [({},None),({'CODEX_THREAD_ID':'fixture'},'codex'),({'CLAUDECODE':'1'},'claude'),
                             ({'CODEX_THREAD_ID':'fixture','CLAUDECODE':'1'},None)]:
            with mock.patch.dict(os.environ,env,clear=True):
                self.assertEqual(tg.Conf(str(self.fx.config)).agent_client,expected)

    def test_legacy_migration_uses_explicit_codex_history_and_preserves_accounts(self):
        path=self.fx.work/'legacy.db'
        with sqlite3.connect(path) as con:
            con.executescript(self.fx.s.SCHEMA)
            con.execute('ALTER TABLE users ADD COLUMN last_check_ts REAL')
            rows=[('alice','human','alice','human-hash',1,1),
                  ('alice-agent','agent','alice','codex-hash',1,1),
                  ('bob-agent','agent','alice','unknown-hash',1,None)]
            con.executemany('INSERT INTO users(name,kind,owner,token_hash,created_ts,last_check_ts) VALUES (?,?,?,?,?,?)',rows)
        con.close()
        spec=importlib.util.spec_from_file_location('avatar_migration',fixture.ROOT/'server.py')
        server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server);server.Cfg.db=str(path)
        try:
            con=server.db()
            clients={r['name']:r['agent_client'] for r in con.execute('SELECT * FROM users')}
            self.assertEqual(clients,{'alice':None,'alice-agent':'codex','bob-agent':None})
            self.assertEqual([tuple(r) for r in con.execute('SELECT name,kind,owner,token_hash,created_ts,last_check_ts FROM users')],rows)
            server._db.close();server._db=None
            self.assertEqual(server.db().execute("SELECT agent_client FROM users WHERE name='alice-agent'").fetchone()[0],'codex')
        finally:
            if server._db:server._db.close()


if __name__=='__main__':unittest.main(verbosity=2)
