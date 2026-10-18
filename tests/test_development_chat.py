"""Private admin scope and a session bridge with real HTTP, without model calls."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import types
import unittest
from unittest import mock
import urllib.error

import test_attachments as fixture


class DevelopmentChatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass(); cls.fx=fixture.AttachmentTest()
        cls.s,cls.work=cls.fx.s,cls.fx.work
        cls.s.db().execute("UPDATE users SET is_admin=1 WHERE name='alice'")
        cls.project=cls.fx.api('POST','/api/admin/development-chat',{},who='alice')['project']['slug']
        cls.token=cls.work/'developer.token'
        with redirect_stdout(io.StringIO()):
            cls.s.cmd_development_agent(types.SimpleNamespace(admin='alice',name='developer',token_out=str(cls.token),rotate=False))
        cls.key=cls.s.db().execute('SELECT service_agent FROM projects WHERE slug=?',(cls.project,)).fetchone()[0]
        cls.fx.tokens[cls.key]=cls.token.read_text().strip()
        invite=cls.fx.api('POST','/api/invites?project=main',{'name':'bob','kind':'human'},who='alice')
        cls.fx.tokens['bob']=cls.fx.api('POST','/api/join',{'code':invite['code'],'password':'bob fixture password'},who=None)['token']
        spec=importlib.util.spec_from_file_location('test_chat_bridge',fixture.ROOT/'tools'/'codex_chat_bridge.py')
        cls.bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.bridge)
        cls.config=cls.work/'developer.json'
        cls.config.write_text(json.dumps({'server':cls.fx.url,'token_file':str(cls.token),'project':cls.project,
                                          'url_source':'','state_file':str(cls.work/'developer.state.json')}),encoding='utf-8')
        cls.conf=cls.bridge.tg.Conf(str(cls.config))

    @classmethod
    def tearDownClass(cls):
        fixture.AttachmentTest.tearDownClass()

    def api(self,method,path,data=None,who='alice',project=True):
        if project:path+=('&' if '?' in path else '?')+'project='+self.project
        return self.fx.api(method,path,data,who=who)

    def error(self,code,method,path,data=None,**kw):
        with self.assertRaises(urllib.error.HTTPError) as raised:self.api(method,path,data,**kw)
        self.assertEqual(raised.exception.code,code,raised.exception.read().decode())

    def setUp(self):
        self.folder=self.work/self.s.secrets.token_hex(8);self.folder.mkdir()
        self.state=self.folder/'status.json'
        self.journal=self.folder/'session.jsonl'
        self.journal.write_text(json.dumps({'type':'event_msg','payload':{'type':'task_complete'}})+'\n')
        self.transcript=self.bridge.Transcript(self.journal)
        self.session='00000000-0000-4000-8000-000000000001'
        incoming=self.api('GET','/api/messages?for_me=1&unacked=1',who=self.key)['messages']
        if incoming:self.api('POST','/api/receipts',{'kind':'ack','ids':[m['id'] for m in incoming]},who=self.key)

    def test_only_server_admin_can_open_and_bind_chat(self):
        for who in ('bob','carol','alice-agent'):
            self.error(403,'POST','/api/admin/development-chat',{},who=who,project=False)
        self.error(401,'POST','/api/admin/development-chat',{},who=None,project=False)
        original=self.api('POST','/api/admin/development-chat',{},project=False)['project']
        self.assertEqual(original['slug'],self.project);self.assertTrue(original['developer_connected'])
        self.assertEqual(original['kind'],'development')
        old=self.token.read_text()
        with redirect_stdout(io.StringIO()):
            self.s.cmd_development_agent(types.SimpleNamespace(admin='alice',name='developer',token_out=str(self.token),rotate=False))
        self.assertEqual(self.token.read_text(),old)
        with self.assertRaises(self.s.ApiError):
            with redirect_stdout(io.StringIO()):
                self.s.cmd_development_agent(types.SimpleNamespace(admin='carol',name='developer',token_out=str(self.folder/'other.token'),rotate=False))

    def test_foreign_participant_cannot_read_even_with_injected_owner_membership(self):
        mid=self.api('POST','/api/messages',{'body':'private admin message','attachments':[{'name':'private.txt','content_b64':'Zml4dHVyZQ=='}]})['id']
        attachment=self.api('GET',f'/api/messages/{mid}')['message']['attachments'][0]['id']
        pid=self.s.db().execute('SELECT id FROM projects WHERE slug=?',(self.project,)).fetchone()[0]
        self.s.db().execute("INSERT INTO members(project_id,user,role) VALUES (?, 'bob','owner')",(pid,))
        try:
            listed=self.api('GET','/api/projects',who='bob',project=False)['projects']
            self.assertNotIn(self.project,[p['slug'] for p in listed])
            for path in ('/api/messages',f'/api/messages/{mid}',f'/api/attachments/{attachment}','/api/board','/api/claims'):
                self.error(404,'GET',path,who='bob')
            self.error(404,'POST',f'/api/messages/{mid}/reaction',{'emoji':'🔥'},who='bob')
            self.error(404,'POST',f'/api/attachments/{attachment}/download',{},who='bob')
            self.error(404,'POST','/api/messages',{'body':'forged'},who='bob')
            self.assertNotIn('bob',[u['name'] for u in self.api('GET','/api/me')['users']])
        finally:self.s.db().execute('DELETE FROM members WHERE project_id=? AND user=?',(pid,'bob'))
        self.error(404,'GET',f'/api/messages/{mid}',who='alice-agent')

    def test_admin_cannot_invite_foreign_people_or_agents_into_this_chat(self):
        self.error(403,'POST','/api/invites',{'name':'another-agent','kind':'agent'})
        self.error(403,'POST','/api/members/add',{'name':'bob','account_id':'bob'})
        self.error(403,'POST','/api/members/add',{'name':'carol','account_id':'carol'})
        self.assertEqual({u['id'] for u in self.api('GET','/api/me')['users']},{'alice',self.key})

    def test_loss_of_admin_role_revokes_private_scope_for_person_and_session(self):
        mid=self.api('POST','/api/messages',{'body':'admin access boundary'})['id']
        self.s.db().execute("UPDATE users SET is_admin=0 WHERE name='alice'")
        try:
            for who in ('alice',self.key):
                self.error(404,'GET',f'/api/messages/{mid}',who=who)
                self.assertNotIn(self.project,[p['slug'] for p in self.api('GET','/api/projects',who=who,project=False)['projects']])
            self.error(403,'POST','/api/admin/development-chat',{},project=False)
        finally:self.s.db().execute("UPDATE users SET is_admin=1 WHERE name='alice'")
        self.assertEqual(self.api('GET',f'/api/messages/{mid}',who=self.key)['message']['body'],'admin access boundary')

    def incoming(self):
        return self.api('POST','/api/messages',{'body':'PEER_TEXT_DO_NOT_INJECT','to':['developer'],'type':'question'})['id']

    def test_bridge_resumes_selected_session_with_metadata_and_replies_in_thread(self):
        mid=self.incoming()
        def fake_codex(argv,**kw):
            self.assertEqual(argv[1:3],['exec','resume'])
            self.assertEqual(argv[-2:], [self.session,'-'])
            for banned in ('PEER_TEXT_DO_NOT_INJECT','--dangerously','--ignore-rules','--ignore-user-config'):
                self.assertNotIn(banned,kw['input'].decode()+str(argv))
            self.assertIn('read',kw['input'].decode());self.assertIn(str(mid),kw['input'].decode())
            Path(argv[argv.index('--output-last-message')+1]).write_text('Ответ этой сессии',encoding='utf-8')
            return types.SimpleNamespace(returncode=0)
        with mock.patch.object(self.bridge.subprocess,'run',side_effect=fake_codex) as run:
            self.assertTrue(self.bridge.step(self.conf,self.config,self.session,self.transcript,'codex',self.folder,self.state))
            self.assertEqual(run.call_count,1)
        result=self.api('GET',f'/api/messages/{mid}',who=self.key)
        reply=next(m for m in result['thread'] if m['body']=='Ответ этой сессии')
        self.assertEqual(reply['author_id'],self.key);self.assertEqual(reply['to_ids'],['alice'])
        self.assertTrue(result['message']['acked'])

    def test_busy_session_and_native_failure_leave_message_pending(self):
        mid=self.incoming()
        with self.journal.open('a') as f:f.write(json.dumps({'type':'event_msg','payload':{'type':'task_started'}})+'\n')
        with mock.patch.object(self.bridge.subprocess,'run') as run,mock.patch.object(self.bridge.time,'sleep'):
            self.assertFalse(self.bridge.step(self.conf,self.config,self.session,self.transcript,'codex',self.folder,self.state))
            run.assert_not_called()
        self.assertFalse(self.api('GET',f'/api/messages/{mid}',who=self.key)['message']['acked'])
        with self.journal.open('a') as f:f.write(json.dumps({'type':'event_msg','payload':{'type':'task_complete'}})+'\n')
        with mock.patch.object(self.bridge.subprocess,'run',return_value=types.SimpleNamespace(returncode=2)):
            with self.assertRaises(RuntimeError):
                self.bridge.step(self.conf,self.config,self.session,self.transcript,'codex',self.folder,self.state)
        self.assertEqual(json.loads(self.state.read_text())['status'],'needs_attention')
        self.assertFalse(self.api('GET',f'/api/messages/{mid}',who=self.key)['message']['acked'])

    def test_bridge_cannot_run_for_an_ordinary_project_agent(self):
        config=self.folder/'normal.json'
        config.write_text(json.dumps({'server':self.fx.url,'token':self.fx.tokens['bob-agent'],'project':'main','url_source':''}))
        with self.assertRaises(RuntimeError):
            self.bridge.step(self.bridge.tg.Conf(str(config)),config,self.session,self.transcript,'codex',self.folder,self.state)


if __name__=='__main__':unittest.main(verbosity=2)
