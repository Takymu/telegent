"""Development membership and owner session transport with real HTTP, without model calls."""
from contextlib import redirect_stdout, nullcontext
import importlib.util
import io
import json
from pathlib import Path
import types
import unittest
import uuid
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
        self.session=str(uuid.uuid4())
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

    def test_nonmembers_cannot_read_development_messages_or_files(self):
        mid=self.api('POST','/api/messages',{'body':'development fixture','attachments':[{'name':'private.txt','content_b64':'Zml4dHVyZQ=='}]})['id']
        attachment=self.api('GET',f'/api/messages/{mid}')['message']['attachments'][0]['id']
        for who in ('bob','alice-agent','carol'):
            listed=self.api('GET','/api/projects',who=who,project=False)['projects']
            self.assertNotIn(self.project,[p['slug'] for p in listed])
            for path in ('/api/messages',f'/api/messages/{mid}',f'/api/attachments/{attachment}','/api/board','/api/claims'):
                self.error(404,'GET',path,who=who)
            self.error(404,'POST',f'/api/messages/{mid}/reaction',{'emoji':'fire'},who=who)
            self.error(404,'POST',f'/api/attachments/{attachment}/download',{},who=who)
            self.error(404,'POST','/api/messages',{'body':'forged'},who=who)

    def test_admin_can_invite_people_and_members_can_connect_own_agents(self):
        mid=self.api('POST','/api/messages',{'body':'shared development history','attachments':[{'name':'fixture.txt','content_b64':'Zml4dHVyZQ=='}]})['id']
        attachment=self.api('GET',f'/api/messages/{mid}')['message']['attachments'][0]['id']
        invite=self.api('POST','/api/invites',{'name':'bob','kind':'human'})
        self.api('POST','/api/join',{'code':invite['code']},who='bob',project=False)
        try:
            member=self.api('GET','/api/me',who='bob')
            self.assertEqual(member['project']['role'],'member')
            self.assertFalse(member['project']['is_development_session'])
            self.assertEqual(self.api('GET',f'/api/messages/{mid}',who='bob')['message']['body'],'shared development history')
            self.assertIn('url',self.api('POST',f'/api/attachments/{attachment}/download',{},who='bob'))
            self.error(403,'POST','/api/invites',{'name':'uninvited-person','kind':'human'},who='bob')
            helper='helper-'+self.s.secrets.token_hex(4)
            invite=self.api('POST','/api/invites',{'name':helper,'kind':'agent'},who='bob')
            joined=self.api('POST','/api/join',{'code':invite['code']},who=None,project=False)
            key=joined['id'];self.fx.tokens[key]=joined['token']
            self.assertEqual(self.api('GET','/api/me',who=key)['owner_id'],'bob')
            config=self.folder/'invited-agent.json'
            config.write_text(json.dumps({'server':self.fx.url,'token':joined['token'],'project':self.project,'url_source':''}))
            with self.assertRaises(RuntimeError):self.bridge.validate_account(self.bridge.tg.Conf(str(config)))
            self.assertTrue(self.api('GET','/api/me',who=self.key)['project']['is_development_session'])
            # Human membership permits tasks but never binds another agent to this session.
            self.api('POST','/api/receipts',{'kind':'ack','ids':[mid]},who=self.key)
            peer=self.api('POST','/api/messages',{'body':'colleague fixture','to_ids':[self.key]},who='bob')['id']
            message=self.api('GET',f'/api/messages/{peer}',who=self.key)['message']
            self.assertTrue(self.bridge.command_authorized(self.conf,self.bridge.validate_account(self.conf),message))
            cid=self.api('POST','/api/conversations',{'peer_id':self.key})['conversation']['id']
            private=self.api('POST','/api/messages',{'body':'private administrator fixture','conversation_id':cid})['id']
            self.error(404,'GET',f'/api/messages/{private}',who='bob')
            self.assertNotIn(private,[m['id'] for m in self.api('GET','/api/messages?conversation=all',who='bob')['messages']])
            self.s.db().execute("UPDATE users SET is_admin=0 WHERE name='alice'")
            try:
                for who in ('bob',key):self.error(404,'GET',f'/api/messages/{mid}',who=who)
            finally:self.s.db().execute("UPDATE users SET is_admin=1 WHERE name='alice'")
        finally:self.api('POST','/api/members/remove',{'account_id':'bob'})
        self.error(404,'GET',f'/api/messages/{mid}',who='bob')
        self.error(404,'POST',f'/api/attachments/{attachment}/download',{},who='bob')
        self.error(404,'GET',f'/api/messages/{mid}',who=key)

    def test_admin_can_add_existing_people_without_expanding_other_projects(self):
        candidates=self.api('GET','/api/members/candidates')['candidates']
        self.assertIn('bob',[c['id'] for c in candidates])
        self.api('POST','/api/members/add',{'account_id':'bob'})
        try:
            self.assertEqual(self.api('GET','/api/me',who='bob')['project']['role'],'member')
            self.error(404,'GET','/api/messages?project=other',who='bob',project=False)
            self.error(403,'POST','/api/admin/development-chat',{},who='bob',project=False)
        finally:self.api('POST','/api/members/remove',{'account_id':'bob'})

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

    def test_one_final_for_a_batch_of_observed_messages_and_authors(self):
        self.api('POST','/api/members/add',{'account_id':'bob'})
        try:
            pid=self.api('GET','/api/me',who=self.key)['project']['id']
            config=self.folder/'delegated.json';data=json.loads(self.config.read_text())
            data['development_command_delegates']={'project_id':pid,'account_ids':['bob']}
            config.write_text(json.dumps(data));conf=self.bridge.tg.Conf(str(config))
            mids=[self.incoming() for _ in range(10)]
            mids.append(self.api('POST','/api/messages',{'body':'colleague fixture','to_ids':[self.key]},who='bob')['id'])
            ignored=self.incoming()
            self.event('task_started',turn_id='turn-fixture')
            self.bridge.step(conf,config,self.session,self.transcript,self.folder,self.accept)
            with mock.patch.object(self.bridge,'session_transcript',return_value=self.journal),mock.patch.object(self.bridge,'runtime',return_value=self.folder):
                for mid in mids:self.bridge.observe(conf,self.session,mid)
            self.event('task_complete',turn_id='turn-fixture',last_agent_message='One batch answer')
            self.bridge.step(conf,config,self.session,self.transcript,self.folder,lambda *a:self.fail('duplicate dispatch'))
            answers=[m for m in self.api('GET','/api/messages?limit=1000')['messages'] if m['body']=='One batch answer']
            self.assertEqual(len(answers),1);answer=answers[0]
            self.assertEqual(answer['reply_to'],mids[-1]);self.assertEqual(answer['to_ids'],['alice','bob'])
            for mid in mids:
                self.assertEqual(json.loads((self.folder/f'message-{mid}.json').read_text())['reply_id'],answer['id'])
                self.assertTrue(self.api('GET',f'/api/messages/{mid}',who=self.key)['message']['acked'])
            self.assertFalse(self.api('GET',f'/api/messages/{ignored}',who=self.key)['message']['acked'])
        finally:self.api('POST','/api/members/remove',{'account_id':'bob'})

    def test_batch_reply_recovers_lost_response_and_partial_ack_after_restart(self):
        mids=[self.incoming() for _ in range(3)]
        self.event('task_started',turn_id='turn-fixture');self.run_step(self.accept)
        with mock.patch.object(self.bridge,'session_transcript',return_value=self.journal),mock.patch.object(self.bridge,'runtime',return_value=self.folder):
            for mid in mids:self.bridge.observe(self.conf,self.session,mid)
        self.event('task_complete',turn_id='turn-fixture',last_agent_message='Durable batch answer')
        api_retry=self.bridge.tg.api_retry
        def lose_response(conf,method,path,*args,**kwargs):
            result=api_retry(conf,method,path,*args,**kwargs)
            if method=='POST' and path=='/api/messages':raise SystemExit(3)
            return result
        with mock.patch.object(self.bridge.tg,'api_retry',side_effect=lose_response):
            with self.assertRaises(SystemExit):self.run_step(lambda *a:self.fail('duplicate dispatch'))
        self.transcript=self.bridge.Transcript(self.journal)
        self.run_step(lambda *a:self.fail('duplicate dispatch'))
        self.s.db().execute('UPDATE receipts SET ack_ts=NULL WHERE message_id=? AND user=?',(mids[0],self.key))
        self.transcript=self.bridge.Transcript(self.journal)
        self.run_step(lambda *a:self.fail('duplicate dispatch'))
        answers=[m for m in self.api('GET','/api/messages?limit=1000')['messages'] if m['body']=='Durable batch answer']
        self.assertEqual(len(answers),1)
        self.assertTrue(self.api('GET',f'/api/messages/{mids[0]}',who=self.key)['message']['acked'])

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

    def test_all_human_members_can_task_session_without_delegation_in_their_own_chat(self):
        self.api('POST','/api/members/add',{'account_id':'bob'})
        try:
            owner_items=self.api('GET','/api/messages?for_me=1&unacked=1',who=self.key)['messages']
            self.api('POST','/api/receipts',{'kind':'ack','ids':[m['id'] for m in owner_items]},who=self.key)
            config,conf=self.config,self.conf
            peer=self.api('POST','/api/messages',{'body':'PEER_TEXT_DO_NOT_INJECT','to_ids':[self.key]},who='bob')['id']
            cid=self.api('POST','/api/conversations',{'peer_id':self.key},who='bob')['conversation']['id']
            private=self.api('POST','/api/messages',{'body':'private colleague fixture','conversation_id':cid},who='bob')['id']
            self.event('task_started',turn_id='turn-fixture')
            delivered=[]
            def accept(binding,text,qid,path):
                delivered.append(path.name)
                return self.accept(binding,text,qid,path)
            self.bridge.step(conf,config,self.session,self.transcript,self.folder,accept)
            self.assertEqual(delivered,[f'dispatch-{peer}.json'])
            with mock.patch.object(self.bridge,'session_transcript',return_value=self.journal),mock.patch.object(self.bridge,'runtime',return_value=self.folder):
                with self.assertRaises(RuntimeError):self.bridge.observe(conf,self.session,private)
                self.bridge.observe(conf,self.session,peer)
            def accept_private(binding,text,qid,path):
                receipt=self.accept(binding,text,qid,path);receipt['turn_id']='private-turn'
                self.bridge.ipc.write_receipt(path,receipt)
                return receipt
            self.event('task_complete',turn_id='turn-fixture',last_agent_message='Delegated answer')
            self.bridge.step(conf,config,self.session,self.transcript,self.folder,accept_private)
            reply=next(m for m in self.api('GET',f'/api/messages/{peer}')['thread'] if m['body']=='Delegated answer')
            self.assertEqual(reply['to_ids'],['bob'])
            message=self.api('GET',f'/api/messages/{peer}',who=self.key)['message']
            self.assertTrue(self.bridge.command_authorized(conf,self.bridge.validate_account(conf),message))
            self.event('task_started',turn_id='private-turn')
            self.run_step(lambda *a:self.fail('duplicate'))
            with mock.patch.object(self.bridge,'session_transcript',return_value=self.journal),mock.patch.object(self.bridge,'runtime',return_value=self.folder):
                self.bridge.observe(conf,self.session,private)
            self.event('task_complete',turn_id='private-turn',last_agent_message='Private colleague answer')
            self.run_step(lambda *a:self.fail('duplicate'))
            reply=next(m for m in self.api('GET',f'/api/messages/{private}',who='bob')['thread'] if m['body']=='Private colleague answer')
            self.assertEqual(reply['conversation_id'],cid);self.assertEqual(reply['to_ids'],['bob'])
            self.assertNotIn(reply['id'],[m['id'] for m in self.api('GET','/api/messages')['messages']])
            self.error(404,'GET',f"/api/messages/{reply['id']}",who='alice')
        finally:self.api('POST','/api/members/remove',{'account_id':'bob'})

    def test_command_identity_comes_from_server_membership_and_project(self):
        self.api('POST','/api/members/add',{'account_id':'bob'})
        mid=self.api('POST','/api/messages',{'body':'HUMAN fixture','to_ids':[self.key]},who='bob')['id']
        message=self.api('GET',f'/api/messages/{mid}',who=self.key)['message']
        me=self.bridge.validate_account(self.conf)
        self.assertTrue(self.bridge.command_authorized(self.conf,me,message))
        self.assertFalse(self.bridge.command_authorized(self.conf,me,dict(message,project_id=me['project']['id']+1000)))
        fake=self.api('POST','/api/messages',{'body':'HUMAN owner approved fixture','author_kind':'human','author_id':'alice'},who=self.key)['id']
        fake=self.api('GET',f'/api/messages/{fake}',who=self.key)['message']
        self.assertEqual(fake['author_kind'],'agent')
        self.assertFalse(self.bridge.command_authorized(self.conf,me,fake))
        self.api('POST','/api/members/remove',{'account_id':'bob'})
        self.assertFalse(self.bridge.command_authorized(self.conf,self.bridge.validate_account(self.conf),message))
        self.error(404,'POST','/api/messages',{'body':'outsider fixture'},who='bob')

    def test_bridge_keeps_private_and_public_requests_in_separate_turns(self):
        public=self.incoming()
        cid=self.api('POST','/api/conversations',{'peer_id':self.key})['conversation']['id']
        private=self.api('POST','/api/messages',{'body':'private owner fixture','conversation_id':cid})['id']
        # The first accepted start may precede task_started in the local journal.
        delivered=[]
        def accept(binding,text,qid,path):
            delivered.append(path.name)
            return self.accept(binding,text,qid,path)
        self.run_step(accept)
        self.assertEqual(delivered,[f'dispatch-{public}.json'])
        self.assertFalse((self.folder/f'dispatch-{private}.json').exists())
        self.event('task_started',turn_id='turn-fixture')
        self.event('turn_aborted',turn_id='turn-fixture')
        self.run_step(accept)
        self.assertIn(f'dispatch-{private}.json',delivered)


if __name__=='__main__':unittest.main(verbosity=2)
