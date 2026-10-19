"""Secretary memberships, isolation, invitations, CLI and repeatable migration over real HTTP."""
import importlib.util
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import unittest
import urllib.error

import test_attachments as fixture


class SecretaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest()
        cls.s,cls.work=cls.fx.s,cls.fx.work

    @classmethod
    def tearDownClass(cls):
        fixture.AttachmentTest.tearDownClass()

    def api(self,method,path,data=None,who='alice',project=None):
        if project:path+=('&' if '?' in path else '?')+'project='+project
        return self.fx.api(method,path,data,who=who)

    def error(self,code,method,path,data=None,**kw):
        with self.assertRaises(urllib.error.HTTPError) as raised:self.api(method,path,data,**kw)
        self.assertEqual(raised.exception.code,code,raised.exception.read().decode())

    def setUp(self):
        self.projects=[];self.keys=[];self.tag=secrets.token_hex(4)
        for suffix in ('one','two'):
            p=self.api('POST','/api/projects',{'title':'Fixture '+suffix,'slug':suffix+'-'+self.tag})['project']
            self.projects.append(p['slug'])

    def tearDown(self):
        for key in self.keys:self.api('POST','/api/secretaries/revoke',{'account_id':key})
        for project in self.projects:self.api('POST','/api/project/delete',{'confirm':project},project=project)

    def invite(self,name=None,**extra):
        return self.api('POST','/api/invites',dict(name=name or 'helper-'+self.tag,kind='agent',is_secretary=True,**extra))

    def join(self,inv):
        joined=self.api('POST','/api/join',{'code':inv['code']},who=None)
        self.keys.append(joined['id']);self.fx.tokens[joined['id']]=joined['token'];return joined

    def helper(self):return self.join(self.invite())

    def test_current_future_projects_and_owner_departure(self):
        joined=self.helper();key=joined['id']
        self.assertTrue(joined['is_secretary']);self.assertIsNone(joined['project'])
        expected={p['slug'] for p in self.api('GET','/api/projects')['projects']}
        self.assertEqual({p['slug'] for p in self.api('GET','/api/projects',who=key)['projects']},expected)
        for p in self.projects:
            me=self.api('GET','/api/me',who=key,project=p)
            self.assertEqual(me['kind'],'agent');self.assertEqual(me['project']['role'],'secretary')
            self.assertEqual(next(u['role'] for u in me['users'] if u['id']==key),'secretary')
        p=self.api('POST','/api/projects',{'title':'Future fixture'})['project']['slug'];self.projects.append(p)
        self.assertEqual(self.api('GET','/api/me',who=key,project=p)['project']['slug'],p)
        # Owner removal closes access even to known historic ids, without removing history.
        mid=self.api('POST','/api/messages',{'body':'historic fixture'},project=p)['id']
        with self.s.DB_LOCK:self.s.db().execute('DELETE FROM members WHERE project_id=(SELECT id FROM projects WHERE slug=?) AND user=?',(p,'alice'))
        self.error(404,'GET',f'/api/messages/{mid}',who=key)
        with self.s.DB_LOCK:self.s.add_member(self.s.db(),self.s.db().execute('SELECT id FROM projects WHERE slug=?',(p,)).fetchone()[0],'alice','owner','alice')
        self.assertEqual(self.api('GET',f'/api/messages/{mid}',who=key)['message']['body'],'historic fixture')

    def test_permissions_private_chats_and_project_boundaries(self):
        key=self.helper()['id'];p=self.projects[0]
        self.error(403,'POST','/api/projects',{'title':'forbidden'},who=key)
        self.error(403,'POST','/api/invites',{'name':'forbidden','kind':'agent','is_secretary':True},who=key)
        self.error(403,'POST','/api/project/rename',{'title':'forbidden'},who=key,project=p)
        self.error(400,'POST','/api/messages',{'body':'ambiguous target'},who=key)
        self.error(404,'GET','/api/messages',who=key,project='other')
        self.error(403,'POST','/api/invites',{'name':'forbidden','kind':'agent','is_secretary':True,'owner_id':'carol'})
        self.error(403,'GET','/api/messages?projects=all',who='alice-agent')
        cid=self.api('POST','/api/conversations',{'peer_id':'alice-agent'},project='main')['conversation']['id']
        private=self.api('POST','/api/messages',{'body':'private owner fixture','conversation_id':cid},project='main')['id']
        self.error(404,'GET',f'/api/messages/{private}',who=key)
        rows=self.api('GET','/api/messages?projects=all&for_me=1&conversation=all',who=key)['messages']
        self.assertNotIn(private,[m['id'] for m in rows])
        self.error(409,'POST','/api/members/remove',{'account_id':key},project=p)
        self.error(400,'POST','/api/members/role',{'account_id':key,'role':'owner'},project=p)
        own=self.api('POST','/api/messages',{'attachments':[{'name':'fixture.txt','content_b64':'Zml4dHVyZQ=='}]},who=key,project=p)['id']
        attachment=self.api('GET',f'/api/messages/{own}',project=p)['message']['attachments'][0]['id']
        with self.fx.request('GET',f'/api/attachments/{attachment}',who=key) as response:self.assertEqual(response.read(),b'fixture')
        self.error(404,'GET',f'/api/attachments/{attachment}',who='carol')

    def test_future_name_collision_rolls_back_owner_join_and_keeps_invitation(self):
        key=self.helper()['id'];name=self.api('GET','/api/me',who=key)['name']
        p=self.api('POST','/api/projects',{'title':'Foreign fixture'},who='carol')['project']['slug']
        try:
            inv=self.api('POST','/api/invites',{'name':name,'kind':'agent'},who='carol',project=p)
            self.api('POST','/api/join',{'code':inv['code']},who=None)
            invite=self.api('POST','/api/invites',{'name':'alice','kind':'human'},who='carol',project=p)
            self.error(409,'POST','/api/join',{'code':invite['code']})
            self.assertIsNone(self.s.db().execute('SELECT 1 FROM members WHERE project_id=(SELECT id FROM projects WHERE slug=?) AND user=?',(p,'alice')).fetchone())
            self.api('POST','/api/rename',{'account_id':key,'new':'renamed-'+self.tag},project='main')
            self.api('POST','/api/join',{'code':invite['code']})
            self.assertEqual(self.api('GET','/api/me',who=key,project=p)['project']['slug'],p)
        finally:self.api('POST','/api/project/delete',{'confirm':p},who='carol',project=p)

    def test_aggregate_inbox_wait_metadata_and_explicit_recipient_scope(self):
        key=self.helper()['id'];mids=[]
        for p in self.projects:
            mid=self.api('POST','/api/messages',{'body':'DO_NOT_INJECT_BODY','to_ids':[key]},project=p)['id'];mids.append(mid)
        rows=self.api('GET','/api/wait?projects=all&after=0&timeout=0&for_me=1&conversation=all',who=key)['messages']
        self.assertTrue(set(mids)<={m['id'] for m in rows})
        self.assertEqual({m['project'] for m in rows if m['id'] in mids},set(self.projects))
        summary=self.api('GET','/api/summary?projects=all&metadata=1&client=codex',who=key)
        self.assertNotIn('DO_NOT_INJECT_BODY',json.dumps(summary))
        self.assertTrue(set(mids)<={m['id'] for m in summary['unacked']})
        for p in self.projects:
            mid=self.api('POST','/api/messages',{'body':'helper reply fixture'},who=key,project=p)['id']
            m=self.api('GET',f'/api/messages/{mid}',project=p)['message']
            self.assertTrue(m['author_is_secretary']);self.assertEqual(m['author_id'],key)

    def test_revoke_only_by_owner_preserves_messages_and_stops_future_enrollment(self):
        key=self.helper()['id'];p=self.projects[0]
        mid=self.api('POST','/api/messages',{'body':'keep helper history'},who=key,project=p)['id']
        self.error(404,'POST','/api/secretaries/revoke',{'account_id':key},who='carol')
        self.error(403,'POST','/api/secretaries/revoke',{'account_id':key},who=key)
        self.api('POST','/api/secretaries/revoke',{'account_id':key})
        self.error(401,'GET','/api/me',who=key)
        self.assertEqual(self.api('GET',f'/api/messages/{mid}',project=p)['message']['body'],'keep helper history')
        self.assertEqual(self.s.db().execute('SELECT COUNT(*) FROM members WHERE user=?',(key,)).fetchone()[0],0)
        self.assertEqual(self.api('GET','/api/secretaries',who='carol')['secretaries'],[])

    def test_storage_and_migration_keep_normal_agents_scoped_and_helper_identity(self):
        joined=self.helper();key=joined['id'];con=self.s.db()
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO members(project_id,user,role,added_by,added_ts) VALUES ((SELECT id FROM projects WHERE slug='other'),?,'agent','carol',0)",(key,))
        with self.assertRaises(sqlite3.IntegrityError):con.execute("UPDATE users SET is_secretary=1 WHERE name='alice-agent'")
        saved=self.work/('migration-'+self.tag+'.db')
        dst=sqlite3.connect(saved);con.backup(dst);dst.close()
        spec=importlib.util.spec_from_file_location('secretary_migration',fixture.ROOT/'server.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.Cfg.db=str(saved)
        for _ in range(2):
            migrated=module.db();row=migrated.execute('SELECT * FROM users WHERE name=?',(key,)).fetchone()
            self.assertEqual(row['token_hash'],self.s.token_hash(joined['token']));self.assertTrue(row['is_secretary'])
            self.assertGreater(migrated.execute('SELECT COUNT(*) FROM members WHERE user=?',(key,)).fetchone()[0],1)
            self.assertEqual(migrated.execute('PRAGMA integrity_check').fetchone()[0],'ok');module._db.close();module._db=None

    def test_cli_join_combined_inbox_and_reply_project_inheritance(self):
        inv=self.invite();cwd=self.work/('client-'+self.tag);cwd.mkdir()
        def run(*args,ok=True,payload=None):
            result=subprocess.run([sys.executable,'-X','utf8',str(fixture.ROOT/'tg.py'),*args],cwd=cwd,env=self.fx.env,text=True,encoding='utf-8',capture_output=True,timeout=25,input=payload)
            if ok:self.assertEqual(result.returncode,0,result.stderr)
            return result
        link=self.fx.url+'/#invite='+inv['code']
        run('connect',link,'--client','codex');run('connect',link,'--client','codex')
        config=json.loads((cwd/'.telegent.json').read_text());key=config['account_id'];self.keys.append(key)
        self.fx.tokens[key]=Path(config['token_file']).read_text().strip()
        self.assertTrue(config['is_secretary']);self.assertNotIn('project',config)
        mids=[self.api('POST','/api/messages',{'body':'inbox fixture','to_ids':[key]},project=p)['id'] for p in self.projects]
        out=run('inbox').stdout
        for p in self.projects:self.assertIn('project='+p,out)
        hook=run('codex','hook',payload=json.dumps({'hook_event_name':'SessionStart','session_id':'secretary-fixture','cwd':str(cwd)})).stdout
        self.assertNotIn('inbox fixture',hook)
        for p in self.projects:self.assertIn('проект='+p,hook)
        run('reply',str(mids[1]),'Inherited reply fixture')
        result=self.api('GET',f'/api/messages/{mids[1]}',project=self.projects[1])
        self.assertEqual(result['thread'][-1]['body'],'Inherited reply fixture')
        self.assertEqual(run('send','ambiguous fixture',ok=False).returncode,2)
        run('--project',self.projects[0],'send','Explicit fixture')
        run('ack','--all')
        self.assertEqual(self.api('GET','/api/messages?projects=all&for_me=1&unacked=1',who=key)['messages'],[])

    def test_reset_invite_keeps_secretary_identity_and_scope(self):
        joined=self.helper();key=joined['id']
        inv=self.api('POST','/api/reset',{'account_id':key},project='main')
        self.assertTrue(inv['is_secretary'])
        self.assertTrue(self.api('GET','/api/invite?code='+inv['code'],who=None)['is_secretary'])
        reset=self.api('POST','/api/join',{'code':inv['code']},who=None)
        self.assertEqual(reset['id'],key);self.assertTrue(reset['is_secretary']);self.assertIsNone(reset['project'])
        self.fx.tokens[key]=reset['token']
        self.assertGreater(len(self.api('GET','/api/projects',who=key)['projects']),1)


if __name__=='__main__':unittest.main(verbosity=2)
