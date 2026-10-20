"""Real HTTP and CLI mentions, stable identities, private boundaries and old history."""
import json
import importlib.util
import sqlite3
from contextlib import closing
import unittest
import urllib.error
import test_attachments as fixture


class MentionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest()
        invite=cls.fx.api('POST','/api/invites?project=main',{'name':'bob','kind':'human'},who='alice')
        cls.fx.tokens['bob']=cls.fx.api('POST','/api/join',{'code':invite['code'],'password':'bob fixture password'},who=None)['token']

    @classmethod
    def tearDownClass(cls):fixture.AttachmentTest.tearDownClass()

    def api(self,method,path,data=None,who='alice',project='main'):
        return self.fx.api(method,path+('&' if '?' in path else '?')+'project='+project,data,who=who)

    def send(self,body,**data):
        return self.api('POST','/api/messages',dict(body=body,**data))['id']

    def read(self,mid,who='alice'):
        return self.api('GET',f'/api/messages/{mid}',who=who)['message']

    def error(self,body,bindings):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.send(body,mention_bindings=bindings)
        self.assertEqual(caught.exception.code,400)

    def test_public_multiple_recipients_waiting_and_actual_cli(self):
        mid=self.send('Hi @alice-agent, @bob-agent. @alice-agent',type='question',to_ids=['alice'])
        msg=self.read(mid)
        self.assertEqual(msg['to_ids'],['alice','alice-agent','bob-agent'])
        self.assertEqual([m['id'] for m in msg['mentions']],['alice-agent','bob-agent','alice-agent'])
        for target in ('alice-agent','bob-agent'):
            inbox=self.api('GET','/api/messages?inbox=1',who=target)['messages']
            self.assertIn(mid,{m['id'] for m in inbox})
            waiting=self.api('GET','/api/messages?waiting_me=1',who=target)['messages']
            self.assertIn(mid,{m['id'] for m in waiting})
        # Being addressed does not make a public message private.
        single=self.send('Only @alice-agent')
        self.assertIn(single,{m['id'] for m in self.api('GET','/api/messages',who='bob-agent')['messages']})
        cli=self.fx.cli('send','CLI mentions @alice-agent','-t','question')
        self.assertEqual(cli.returncode,0,cli.stderr)
        latest=next(m for m in self.api('GET','/api/messages')['messages'] if m['body']=='CLI mentions @alice-agent')
        self.assertEqual(latest['to_ids'],['alice-agent'])
        human=self.send('@bob, could you answer?',type='question')
        self.assertEqual(self.read(human)['to_ids'],['bob'])
        self.assertIn(human,{m['id'] for m in self.api('GET','/api/messages?waiting_me=1',who='bob')['messages']})

    def test_code_email_quotes_links_and_foreign_names_remain_text(self):
        examples=['mail@alice-agent','\\@alice-agent','`@alice-agent`','`` @alice-agent ``',
                  '```python\n@alice-agent\n```','~~~\n@alice-agent\n~~~','    @alice-agent',
                  '> @alice-agent','https://example.org/@alice-agent','www.example.org/@alice-agent',
                  '[visit @alice-agent](https://example.org)','<span title="@alice-agent">',
                  '@alice-agent.other','@carol','@missing','a_@alice-agent','/path/@alice-agent']
        for text in examples:
            with self.subTest(text=text):
                msg=self.read(self.send(text,to_ids=['bob-agent']))
                self.assertEqual(msg['mentions'],[])
                self.assertEqual(msg['to_ids'],['bob-agent'])
        self.assertEqual(self.read(self.send('😀 (@ALICE-agent).'))['to_ids'],['alice-agent'])
        for bindings in ([],False,'alice-agent',{'carol':'carol'},{'@alice-agent':'alice-agent'},{'alice-agent':[]}):
            self.error('@alice-agent',bindings)

    def test_same_name_resolves_in_selected_project_and_binding_survives_rename(self):
        projects=[]
        try:
            keys=[]
            for name in ('One fixture','Two fixture'):
                p=self.api('POST','/api/projects',{'title':name})['project']['slug'];projects.append(p)
                inv=self.api('POST','/api/invites',{'name':'worker','kind':'agent'},project=p)
                key=self.api('POST','/api/join',{'code':inv['code']},who=None,project=p)['id'];keys.append(key)
                mid=self.api('POST','/api/messages',{'body':'Hello @worker'},project=p)['id']
                self.assertEqual(self.api('GET',f'/api/messages/{mid}',project=p)['message']['to_ids'],[key])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.api('POST','/api/messages',{'body':'@worker','mention_bindings':{'worker':keys[1]}},project=projects[0])
            self.assertEqual(caught.exception.code,400)
            self.api('POST','/api/rename',{'account_id':keys[0],'new':'renamed-worker'},project=projects[0])
            inv=self.api('POST','/api/invites',{'name':'worker','kind':'agent'},project=projects[0])
            replacement=self.api('POST','/api/join',{'code':inv['code']},who=None,project=projects[0])['id']
            self.assertNotEqual(replacement,keys[0])
            mid=self.api('POST','/api/messages',{'body':'Hello @worker','mention_bindings':{'worker':keys[0]}},project=projects[0])['id']
            msg=self.api('GET',f'/api/messages/{mid}',project=projects[0])['message']
            self.assertEqual(msg['to_ids'],[keys[0]])
            self.assertEqual(msg['mentions'][0]['name'],'renamed-worker')
            self.assertEqual(msg['mentions'][0]['text'],'@worker')
        finally:
            for p in projects:self.api('POST','/api/project/delete',{'confirm':p},project=p)

    def test_private_mentions_never_add_a_third_participant(self):
        cid=self.api('POST','/api/conversations',{'peer_id':'alice-agent'})['conversation']['id']
        mid=self.send('@alice @alice-agent @bob-agent',conversation_id=cid)
        msg=self.read(mid)
        self.assertEqual(msg['to_ids'],['alice-agent'])
        self.assertEqual({m['id'] for m in msg['mentions']},{'alice','alice-agent'})
        with self.assertRaises(urllib.error.HTTPError) as caught:self.read(mid,who='bob-agent')
        self.assertEqual(caught.exception.code,404)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.send('@bob-agent',conversation_id=cid,mention_bindings={'bob-agent':'bob-agent'})
        self.assertEqual(caught.exception.code,400)

    def test_editing_does_not_redeliver_or_change_original_addresses(self):
        mid=self.send('@alice-agent question',type='question')
        edited=self.api('POST',f'/api/messages/{mid}/edit',{'body':'Now @bob-agent and @alice-agent','revision':0})['message']
        self.assertEqual(edited['to_ids'],['alice-agent'])
        self.assertEqual([m['id'] for m in edited['mentions']],['alice-agent'])
        deleted=self.api('POST',f'/api/messages/{mid}/delete',{'revision':1})['message']
        self.assertEqual(deleted['mentions'],[])

    def test_migration_does_not_address_old_text_retroactively(self):
        path=self.fx.work/'legacy-mentions.db'
        with closing(sqlite3.connect(path)) as con:
            con.executescript(self.fx.s.SCHEMA)
            con.execute("INSERT INTO messages(ts,author,author_kind,owner,type,subject,body) VALUES (?,?,?,?,?,?,?)",
                        (self.fx.s.time.time(),'alice','human','alice','info','old','old @alice-agent'))
            con.commit()
        spec=importlib.util.spec_from_file_location('mentions_migration',fixture.ROOT/'server.py')
        s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s);s.Cfg.db=str(path)
        for _ in range(2):
            row=s.db().execute('SELECT * FROM messages WHERE id=1').fetchone()
            self.assertEqual(row['mentions_json'],'[]');self.assertEqual(row['body'],'old @alice-agent')
            self.assertEqual(row['recipients'],'')
            s._db.close();s._db=None

    def test_recent_activity_is_distinct_from_listening_and_working(self):
        s=self.fx.s;now=s.time.time()
        with s.DB_LOCK:
            s.db().execute("UPDATE users SET last_seen_ts=?,last_check_ts=?,last_wait_ts=NULL WHERE name='bob-agent'",(now,now-7200))
            row=s.db().execute("SELECT * FROM users WHERE name='bob-agent'").fetchone()
            p=s.presence(row,now)
            self.assertTrue(p['online']);self.assertFalse(p['listening']);self.assertIsNone(p['working'])
            self.assertFalse(s.presence(row,now+s.LISTEN_WINDOW+1)['online'])
        self.api('POST','/api/messages',{'body':'agent activity fixture'},who='alice-agent')
        u=next(u for u in self.api('GET','/api/me')['users'] if u['id']=='alice-agent')
        self.assertTrue(u['online']);self.assertFalse(u['listening'])


if __name__=='__main__':unittest.main(verbosity=2)
