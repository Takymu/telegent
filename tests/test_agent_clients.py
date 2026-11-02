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
            self.fx.s.db().execute('UPDATE users SET agent_client=NULL,agent_client_selected=0')
        config = json.loads(self.fx.config.read_text())
        config.pop('client', None); config.pop('agent_client',None); config['auto_update'] = False
        self.fx.config.write_text(json.dumps(config))
        for key in ('CODEX_THREAD_ID','CODEX_SESSION_ID','CLAUDECODE'):
            self.fx.env.pop(key, None)

    def test_only_authenticated_agent_updates_its_display_hint(self):
        for client in ('codex','claude','kimi'):
            me = self.fx.api('GET','/api/me',headers={'X-Telegent-Client':client})
            self.assertEqual(me['agent_client'],client)
            self.assertEqual(me['kind'],'agent'); self.assertFalse(me['is_admin'])
            other = next(u for u in me['users'] if u['id']=='alice-agent')
            self.assertIsNone(other['agent_client'])
            self.assertEqual(me['project']['role'],'agent')
        self.assertEqual(self.fx.api('GET','/api/me',headers={'X-Telegent-Client':'human'})['agent_client'],'kimi')
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

    def test_kimi_client_choice_preserves_config_hooks_and_identity(self):
        original=json.loads(self.fx.config.read_text());original['client']='claude';original['custom']={'fixture':True}
        self.fx.config.write_text(json.dumps(original))
        hooks=self.fx.work/'.claude/settings.local.json';hooks.parent.mkdir(exist_ok=True)
        hooks.write_text('{"fixture":"unchanged"}')
        before=hooks.read_bytes()
        result=self.fx.cli('client','kimi')
        self.assertEqual(result.returncode,0,result.stderr)
        saved=json.loads(self.fx.config.read_text());self.assertEqual(saved,{**original,'agent_client':'kimi'})
        self.assertEqual(hooks.read_bytes(),before)
        for command in [('whoami',),('hook',)]:
            self.assertEqual(self.fx.cli(*command).returncode,0)
            self.assertEqual(self.fx.api('GET','/api/me')['agent_client'],'kimi')
        self.fx.api('GET','/api/summary?metadata=1&client=codex',headers={'X-Telegent-Client':'kimi'})
        self.assertEqual(self.fx.api('GET','/api/me')['agent_client'],'kimi')
        self.assertEqual(self.fx.api('GET','/api/me')['id'],'bob-agent')

    def test_human_cannot_set_agent_client_or_change_its_config(self):
        config=json.loads(self.fx.config.read_text())
        token=self.fx.work/'human-client.token';token.write_text(self.fx.tokens['alice'])
        config['token_file']=str(token)
        path=self.fx.work/'human-client.json';path.write_text(json.dumps(config));before=path.read_bytes()
        result=subprocess.run([__import__('sys').executable,str(fixture.ROOT/'tg.py'),'--config',str(path),'client','kimi'],
                              env=self.fx.env,cwd=self.fx.work,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,2)
        self.assertEqual(path.read_bytes(),before)
        self.assertIsNone(self.fx.api('GET','/api/me',who='alice')['agent_client'])

    def test_selected_client_survives_stale_hook_and_watcher_reports(self):
        stale=self.fx.s.db().execute("SELECT * FROM users WHERE name='bob-agent'").fetchone()
        self.fx.api('POST','/api/client',{'client':'kimi'})
        self.assertEqual(self.fx.s.report_agent_client(stale,'codex')['agent_client'],'kimi')
        for hint in ('codex','claude','kimi','codex'):
            me=self.fx.api('GET','/api/me',headers={'X-Telegent-Client':hint})
            self.assertEqual(me['agent_client'],'kimi');self.assertTrue(me['agent_client_selected'])
        self.fx.api('GET','/api/summary?metadata=1&client=codex')
        self.assertEqual(self.fx.api('GET','/api/me')['agent_client'],'kimi')
        self.fx.api('POST','/api/client',{'client':'claude'})
        self.assertEqual(self.fx.api('GET','/api/me',headers={'X-Telegent-Client':'kimi'})['agent_client'],'claude')

    def test_old_kimi_report_recovers_a_client_previously_overwritten_by_codex(self):
        self.fx.api('GET','/api/me',headers={'X-Telegent-Client':'codex'})
        self.fx.api('GET','/api/me',headers={'X-Telegent-Client':'kimi'})
        me=self.fx.api('GET','/api/me',headers={'X-Telegent-Client':'codex'})
        self.assertEqual(me['agent_client'],'kimi');self.assertTrue(me['agent_client_selected'])

    def test_client_selection_is_self_only_and_does_not_accept_extra_authority(self):
        for payload in ({'client':'kimi','account_id':'alice-agent'},{'client':'human'},{}, {'client':['kimi']}):
            self.fx.error(400,'POST','/api/client',payload)
        self.fx.error(403,'POST','/api/client',{'client':'kimi'},who='alice')
        self.fx.error(401,'POST','/api/client',{'client':'kimi'},who=None)
        self.assertIsNone(self.fx.api('GET','/api/me',who='alice-agent')['agent_client'])

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

    def test_migration_keeps_existing_kimi_selection_and_authentication(self):
        path=self.fx.work/'legacy-kimi.db'
        con=sqlite3.connect(path);con.executescript(self.fx.s.SCHEMA)
        con.execute('ALTER TABLE users ADD COLUMN agent_client TEXT')
        con.executemany('INSERT INTO users(name,kind,owner,token_hash,created_ts) VALUES(?,?,?,?,?)',
                        [('alice','human','alice','human-fixture',1),('worker','agent','alice','agent-fixture',1)])
        con.execute("UPDATE users SET agent_client='kimi' WHERE name='worker'");con.commit();con.close()
        spec=importlib.util.spec_from_file_location('kimi_selection_migration',fixture.ROOT/'server.py')
        server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server);server.Cfg.db=str(path)
        try:
            row=server.db().execute("SELECT * FROM users WHERE name='worker'").fetchone()
            self.assertEqual(row['agent_client'],'kimi');self.assertEqual(row['agent_client_selected'],1)
            self.assertEqual(row['token_hash'],'agent-fixture')
            self.assertEqual(server.report_agent_client(row,'codex')['agent_client'],'kimi')
            server._db.close();server._db=None
            self.assertEqual(server.db().execute("SELECT agent_client_selected FROM users WHERE name='worker'").fetchone()[0],1)
        finally:
            if server._db:server._db.close()
    def test_connect_starts_and_restarts_as_kimi_without_codex_hooks(self):
        root=self.fx.work/'kimi-connect';root.mkdir()
        (root/'AGENTS.md').write_text('custom project instructions\n',encoding='utf-8')
        invite=self.fx.api('POST','/api/invites',{'name':'kimi-connect','kind':'agent'},who='alice')
        link=self.fx.url+'/#invite='+invite['code']
        for _ in range(2):
            result=subprocess.run([__import__('sys').executable,str(fixture.ROOT/'tg.py'),'connect',link,'--client','kimi'],
                                  cwd=root,env=self.fx.env,capture_output=True,text=True,encoding='utf-8',timeout=30)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            config=json.loads((root/'.telegent.json').read_text())
            self.assertEqual(config['client'],'kimi');self.assertEqual(config['agent_client'],'kimi')
            with open(config['token_file'],encoding='utf-8') as stream:
                token=stream.read().strip()
            me=self.fx.api('GET','/api/me',headers={'Authorization':'Bearer '+token})
            self.assertEqual(me['agent_client'],'kimi');self.assertTrue(me['agent_client_selected'])
            self.assertFalse((root/'.codex/hooks.json').exists())
            self.assertIn('custom project instructions', (root/'AGENTS.md').read_text())
            self.assertIn('ACP listener', (root/'AGENTS.md').read_text())


if __name__=='__main__':unittest.main(verbosity=2)
