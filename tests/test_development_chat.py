"""Private admin scope and a session bridge with real HTTP, without model calls."""
from contextlib import redirect_stdout, nullcontext
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

    def event(self, kind, **kw):
        with self.journal.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'type':'event_msg','payload':dict(type=kind, **kw)})+'\n')

    def run_step(self, deliver):
        return self.bridge.step(self.conf,self.config,self.session,self.transcript,self.folder,deliver)

    def accept(self,binding,text,qid,path):
        self.assertNotIn('PEER_TEXT_DO_NOT_INJECT',text)
        self.assertNotIn('exec resume',text)
        self.assertIn('read',text);self.assertIn('--observe',text)
        receipt={'status':'accepted','thread':self.session,'turn_id':'turn-fixture','mode':'steer'}
        self.bridge.ipc.write_receipt(path,receipt)
        return receipt

    def test_direct_dispatch_requires_model_observation_before_forwarding(self):
        mid=self.incoming()
        self.event('task_started',turn_id='turn-fixture')
        self.run_step(self.accept)
        self.assertTrue(self.api('GET','/api/me',who=self.key)['working'])
        self.assertFalse(self.api('GET',f'/api/messages/{mid}',who=self.key)['message']['acked'])
        with mock.patch.object(self.bridge,'session_transcript',return_value=self.journal),mock.patch.object(self.bridge,'runtime',return_value=self.folder):
            self.bridge.observe(self.conf,self.session,mid)
        self.event('task_complete',turn_id='turn-fixture',last_agent_message='Session answer')
        with mock.patch.object(self.bridge.ipc,'deliver',side_effect=AssertionError('duplicate dispatch')):
            self.run_step(lambda *a: self.fail('duplicate dispatch'))
        self.assertFalse(self.api('GET','/api/me',who=self.key)['working'])
        result=self.api('GET',f'/api/messages/{mid}',who=self.key)
        reply=next(m for m in result['thread'] if m['body']=='Session answer')
        self.assertEqual(reply['author_id'],self.key);self.assertEqual(reply['to_ids'],['alice'])
        self.assertTrue(result['message']['acked'])
        # A lost acknowledgement must retry the same reply idempotently, not duplicate it.
        self.s.db().execute('UPDATE receipts SET ack_ts=NULL WHERE message_id=? AND user=?',(mid,self.key))
        self.run_step(lambda *a: self.fail('duplicate dispatch'))
        result=self.api('GET',f'/api/messages/{mid}',who=self.key)
        self.assertEqual(len([m for m in result['thread'] if m['body']=='Session answer']),1)

    def test_busy_session_receives_and_native_failure_preserves_pending(self):
        mid=self.incoming();self.event('task_started',turn_id='turn-fixture')
        def fail(*args):raise self.bridge.ipc.RpcError('unavailable')
        self.run_step(fail)
        self.assertEqual(json.loads(self.state.read_text())['status'],'delivery_pending')
        self.assertFalse(self.api('GET',f'/api/messages/{mid}',who=self.key)['message']['acked'])
        self.run_step(self.accept)
        self.assertTrue(self.api('GET','/api/me',who=self.key)['listening'])
        # Accepted but not observed is never forwarded, even if that turn later ends.
        self.event('task_complete',turn_id='turn-fixture',last_agent_message='Unobserved answer')
        self.run_step(lambda *a:self.fail('duplicate'))
        self.assertFalse(self.api('GET',f'/api/messages/{mid}',who=self.key)['message']['acked'])

    def test_observation_rejects_different_turn(self):
        mid=self.incoming();self.event('task_started',turn_id='another-turn')
        self.run_step(self.accept)
        with mock.patch.object(self.bridge,'session_transcript',return_value=self.journal),mock.patch.object(self.bridge,'runtime',return_value=self.folder):
            with self.assertRaises(RuntimeError):self.bridge.observe(self.conf,self.session,mid)

    def test_exhausted_client_retry_keeps_listener_recoverable(self):
        argv=['bridge','--config',str(self.config),'--session',self.session,'--once']
        with mock.patch.object(self.bridge.sys,'argv',argv), mock.patch.object(self.bridge,'session_transcript',return_value=self.journal), \
             mock.patch.object(self.bridge,'runtime',return_value=self.folder), mock.patch.object(self.bridge.ipc,'check_owner'), \
             mock.patch.object(self.bridge,'session_lock',return_value=nullcontext()), mock.patch.object(self.bridge.time,'sleep'), \
             mock.patch.object(self.bridge,'step',side_effect=SystemExit(3)):
            self.assertEqual(self.bridge.main(),0)
        status=json.loads(self.state.read_text())
        self.assertEqual(status['status'],'offline');self.assertEqual(status['client_exit'],3)

    def test_bridge_cannot_run_for_an_ordinary_project_agent(self):
        config=self.folder/'normal.json'
        config.write_text(json.dumps({'server':self.fx.url,'token':self.fx.tokens['bob-agent'],'project':'main','url_source':''}))
        with self.assertRaises(RuntimeError):
            self.bridge.step(self.bridge.tg.Conf(str(config)),config,self.session,self.transcript,self.folder)


if __name__=='__main__':unittest.main(verbosity=2)
