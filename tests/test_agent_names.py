"""Project-scoped names, stable identities and legacy database compatibility."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
import urllib.error

import test_attachments as fixture


class AgentNamesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass()
        cls.fx = fixture.AttachmentTest()
        cls.s, cls.work = cls.fx.s, cls.fx.work
        invite = cls.fx.api('POST', '/api/invites?project=main', {'name':'bob','kind':'human'}, who='alice')
        cls.fx.tokens['bob'] = cls.fx.api('POST', '/api/join', {'code':invite['code'],'password':'bob fixture password'}, who=None)['token']

    @classmethod
    def tearDownClass(cls):
        fixture.AttachmentTest.tearDownClass()

    def api(self, method, path, data=None, who='alice', project=None):
        if project:
            path += ('&' if '?' in path else '?')+'project='+project
        return self.fx.api(method, path, data, who=who)

    def error(self, code, method, path, data=None, **kw):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.api(method,path,data,**kw)
        self.assertEqual(raised.exception.code,code,raised.exception.read().decode())

    def setUp(self):
        self.projects = []
        for title in ('Team one','Team two'):
            p=self.api('POST','/api/projects',{'title':title})['project'];self.projects.append(p)
            self.api('POST','/api/members/add',{'name':'bob','account_id':'bob'},project=p['slug'])
        self.a,self.b=[p['slug'] for p in self.projects]
        self.first=self.agent(self.a,'worker');self.second=self.agent(self.b,'worker')

    def tearDown(self):
        for p in self.projects:
            self.api('POST','/api/project/delete',{'confirm':p['slug']},project=p['slug'])

    def agent(self,project,name,who='alice'):
        inv=self.api('POST','/api/invites',{'name':name,'kind':'agent'},who=who,project=project)
        joined=self.api('POST','/api/join',{'code':inv['code']},who=None)
        self.fx.tokens[joined['id']]=joined['token']
        return joined['id']

    def test_same_names_in_different_projects_and_case_insensitive_collision_inside_one(self):
        self.assertNotEqual(self.first,self.second)
        for project,key in ((self.a,self.first),(self.b,self.second)):
            me=self.api('GET','/api/me',who=key)
            self.assertEqual(me['name'],'worker');self.assertEqual(me['id'],key)
            self.assertEqual(me['project']['slug'],project)
            self.error(409,'POST','/api/invites',{'name':'WORKER','kind':'agent'},project=project)
        # A regular member can create an alias already used in another project by somebody else's agent.
        self.agent(self.a,'peer-name')
        self.agent(self.b,'peer-name',who='bob')

    def test_recipients_history_receipts_claims_and_files_do_not_cross_projects(self):
        mids=[]
        for project,key in ((self.a,self.first),(self.b,self.second)):
            mid=self.api('POST','/api/messages',{'body':project,'to':['worker']},project=project)['id'];mids.append(mid)
            msg=self.api('GET',f'/api/messages/{mid}',who=key)['message']
            self.assertEqual(msg['to_ids'],[key]);self.assertEqual(msg['to'],['worker'])
            self.api('POST','/api/claims',{'key':'same claim'},who=key)
            self.api('POST','/api/board',{'now':project},who=key)
        self.error(404,'GET',f'/api/messages/{mids[1]}',who=self.first)
        self.error(404,'GET','/api/messages',who=self.first,project=self.b)
        self.error(400,'POST','/api/messages',{'body':'foreign id','to_ids':[self.second]},project=self.a)
        sent=self.api('POST','/api/messages',{'body':'agent identity'},who=self.first)['id']
        msg=self.api('GET',f'/api/messages/{sent}',project=self.a)['message']
        self.assertEqual(msg['author'],'worker');self.assertEqual(msg['author_id'],self.first)
        claims=self.api('GET','/api/claims',who=self.first)['claims']
        self.assertEqual(claims[0]['owner_id'],self.first)
        other=self.api('GET',f'/api/messages/{mids[1]}',who=self.second)['message']
        self.assertFalse(other['read']);self.assertFalse(other['acked'])
        with self.s.DB_LOCK:
            self.assertEqual(self.s.db().execute('SELECT COUNT(*) FROM claims WHERE owner=?',(self.first,)).fetchone()[0],1)

    def test_rename_keeps_id_token_and_history_and_does_not_touch_namesake(self):
        mid=self.api('POST','/api/messages',{'body':'before rename'},who=self.first)['id']
        renamed=self.api('POST','/api/rename',{'name':'worker','new':'renamed-worker'},project=self.a)
        self.assertEqual(renamed['id'],self.first)
        self.assertEqual(self.api('GET','/api/me',who=self.first)['name'],'renamed-worker')
        self.assertEqual(self.api('GET','/api/me',who=self.second)['name'],'worker')
        msg=self.api('GET',f'/api/messages/{mid}',project=self.a)['message']
        self.assertEqual(msg['author_id'],self.first);self.assertEqual(msg['author'],'renamed-worker')
        replacement=self.agent(self.a,'worker')
        self.assertNotEqual(replacement,self.first)
        self.error(409,'POST','/api/rename',{'account_id':self.first,'new':'worker'},project=self.a)
        self.error(404,'POST','/api/rename',{'account_id':self.first,'new':'foreign'},project=self.b)
        self.error(403,'POST','/api/rename',{'account_id':replacement,'new':'foreign'},who='bob',project=self.a)
        config=self.work/'reply-config.json'
        config.write_text(json.dumps({'server':self.fx.url,'token':self.fx.tokens['alice'],'project':self.a,'url_source':''}),encoding='utf-8')
        cli=subprocess.run([sys.executable,'-X','utf8',str(fixture.ROOT/'tg.py'),'--config',str(config),'reply',str(mid),'reply by identity'],
                           env=self.fx.env,cwd=self.work,capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(cli.returncode,0,cli.stderr)
        replies=self.api('GET',f'/api/messages/{mid}',project=self.a)['thread']
        reply=next(m for m in replies if m['body']=='reply by identity')
        self.assertEqual(reply['to_ids'],[self.first])

    def test_independent_reset_links_bind_to_exact_account(self):
        links=[]
        for project in (self.a,self.b):
            links.append(self.api('POST','/api/reset',{'name':'worker'},project=project)['code'])
        self.assertNotEqual(*links)
        first=self.api('POST','/api/join',{'code':links[0]},who=None)
        self.assertEqual(first['id'],self.first)
        self.error(401,'GET','/api/me',who=self.first)
        self.assertEqual(self.api('GET','/api/me',who=self.second)['name'],'worker')
        second=self.api('POST','/api/join',{'code':links[1]},who=None)
        self.assertEqual(second['id'],self.second)
        self.fx.tokens[self.first]=first['token'];self.fx.tokens[self.second]=second['token']
        self.error(404,'POST','/api/reset',{'account_id':self.second},project=self.a)

    def test_two_namesakes_join_from_same_machine_without_overwriting_token_or_state(self):
        configs=[]
        for index,project in enumerate((self.a,self.b)):
            invite=self.api('POST','/api/invites',{'name':'cli-worker','kind':'agent'},project=project)
            folder=self.work/('client-'+str(index));folder.mkdir()
            command=[sys.executable,'-X','utf8',str(fixture.ROOT/'tg.py'),'join',self.fx.url+'/#invite='+invite['code'],
                     '--here','--url-source','']
            result=subprocess.run(command,cwd=folder,env=self.fx.env,capture_output=True,text=True,encoding='utf-8',timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            configs.append(json.loads((folder/'.telegent.json').read_text()))
        self.assertNotEqual(configs[0]['account_id'],configs[1]['account_id'])
        self.assertNotEqual(configs[0]['token_file'],configs[1]['token_file'])
        self.assertNotEqual(configs[0]['state_file'],configs[1]['state_file'])
        self.assertNotEqual(Path(configs[0]['token_file']).read_text(),Path(configs[1]['token_file']).read_text())

    def test_pending_names_are_scoped_and_database_rejects_ambiguous_members(self):
        self.api('POST','/api/invites',{'name':'reserved','kind':'agent'},project=self.b)
        self.api('POST','/api/rename',{'account_id':self.first,'new':'reserved'},project=self.a)
        self.error(409,'POST','/api/rename',{'account_id':self.second,'new':'reserved'},project=self.b)
        with self.s.DB_LOCK:
            con=self.s.db()
            with self.assertRaises(self.s.sqlite3.IntegrityError):
                con.execute('INSERT INTO members(project_id,user,role) VALUES (?,?,?)',(self.projects[0]['id'],self.second,'agent'))
            with self.assertRaises(self.s.sqlite3.IntegrityError):
                con.execute('INSERT INTO users(name,display_name,kind,owner,token_hash,created_ts,agent_project_id) VALUES (?,?,?,?,?,?,?)',
                            ('duplicate-fixture','RESERVED','agent','alice','fixture-unique-hash',1,self.projects[0]['id']))

    def test_detached_namesakes_require_identity_selection(self):
        for project in (self.a,self.b):
            self.api('POST','/api/members/remove',{'name':'worker'},project=project)
        self.error(409,'POST','/api/rename',{'name':'worker','new':'ambiguous'},project=self.a)
        candidates=self.api('GET','/api/members/candidates',project=self.a)['candidates']
        self.assertEqual({c['id'] for c in candidates if c['id'] in (self.first,self.second)},{self.first,self.second})
        self.api('POST','/api/members/add',{'account_id':self.first},project=self.a)
        self.error(409,'POST','/api/members/add',{'account_id':self.second},project=self.a)
        self.assertEqual(self.api('GET','/api/me',who=self.first)['project']['slug'],self.a)
        self.assertIsNone(self.api('GET','/api/me',who=self.second)['project'])

    def test_human_login_is_global_but_agent_name_is_not(self):
        self.api('POST','/api/members/remove',{'name':'bob'},project=self.a)
        key=self.agent(self.a,'bob')
        login=self.api('POST','/api/login',{'name':'bob','password':'bob fixture password'},who=None)
        self.assertEqual(login['kind'],'human');self.assertNotEqual(login['id'],key)
        self.error(409,'POST','/api/members/add',{'name':'bob'},project=self.a)
        self.error(409,'POST','/api/rename',{'name':'alice','new':'worker'},project=self.b)

    def test_legacy_migration_preserves_rows_tokens_and_unused_invites(self):
        folder=self.work/'legacy';folder.mkdir(exist_ok=True)
        old_source=subprocess.check_output(['git','show','e70a19f:server.py'],cwd=fixture.ROOT)
        (folder/'server.py').write_bytes(old_source)
        old_spec=importlib.util.spec_from_file_location('old_identity_server',folder/'server.py')
        old=importlib.util.module_from_spec(old_spec);old_spec.loader.exec_module(old)
        old.Cfg.db=str(folder/'legacy.db');con=old.db()
        token='tgt_'+self.s.secrets.token_hex(24)
        con.execute('INSERT INTO users(name,kind,owner,token_hash,created_ts) VALUES (?,?,?,?,?)',('alice','human','alice',old.token_hash(token),1))
        pid=old.create_project_row(con,'legacy','Legacy','alice');old.add_member(con,pid,'alice','owner','alice')
        agent_token='tgt_'+self.s.secrets.token_hex(24)
        con.execute('INSERT INTO users(name,kind,owner,token_hash,created_ts) VALUES (?,?,?,?,?)',('worker','agent','alice',old.token_hash(agent_token),2))
        old.add_member(con,pid,'worker','agent','alice')
        mid=old.insert_message(con.execute("SELECT * FROM users WHERE name='worker'").fetchone(),'fixture',{'body':'Old history','to':['alice']},pid)[0]
        invitation=old.create_invite('alice','worker',reset=True)
        snapshot={t:con.execute('SELECT * FROM '+t).fetchall() for t in ('users','members','messages','attachments','invites')}
        columns={t:[r[1] for r in con.execute('PRAGMA table_info('+t+')')] for t in snapshot};con.close()
        spec=importlib.util.spec_from_file_location('new_identity_server',fixture.ROOT/'server.py')
        new=importlib.util.module_from_spec(spec);spec.loader.exec_module(new);new.Cfg.db=str(folder/'legacy.db')
        try:
            upgraded=new.db()
            for table,rows in snapshot.items():
                self.assertEqual([tuple(r) for r in rows],[tuple(r) for r in upgraded.execute('SELECT '+','.join(columns[table])+' FROM '+table)])
            self.assertEqual(upgraded.execute('SELECT name FROM users WHERE token_hash=?',(new.token_hash(agent_token),)).fetchone()[0],'worker')
            self.assertEqual(new.msg_dicts([new.get_message_row(mid)])[0]['author_id'],'worker')
            joined=new.redeem_invite(invitation['code']);self.assertEqual(joined['id'],'worker')
            upgraded.close();new._db=None;self.assertEqual(new.db().execute('SELECT COUNT(*) FROM users').fetchone()[0],2)
        finally:
            if new._db is not None:new._db.close()


if __name__=='__main__':
    unittest.main(verbosity=2)
