"""Session binding, durable delivery, restart recovery and conversation separation."""
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
import uuid
from unittest import mock

import test_attachments as fixture

spec=importlib.util.spec_from_file_location('test_receiver',fixture.ROOT/'tools/codex_listener.py')
receiver=importlib.util.module_from_spec(spec);spec.loader.exec_module(receiver)


class ReceiverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest()
    @classmethod
    def tearDownClass(cls):fixture.AttachmentTest.tearDownClass()

    def setUp(self):
        self.root=self.fx.work/uuid.uuid4().hex;self.root.mkdir()
        self.config=self.root/'client.json'
        self.config.write_text(json.dumps({'server':self.fx.url,'token':self.fx.tokens['bob-agent'],'project':'main',
            'url_source':'','auto_update':False,'state_file':str(self.root/'state.json')}))
        self.conf=receiver.tg.Conf(str(self.config));self.folder=receiver.runtime(self.conf)
        self.session=str(uuid.uuid4());self.journal=self.root/'session.jsonl'
        self.journal.write_text(json.dumps({'type':'session_meta','payload':{'id':self.session,'cwd':str(self.root)}})+'\n')
        self.event('task_complete',turn_id='previous')
        self.transcript=receiver.bridge.Transcript(self.journal)
        self.binding={'thread':self.session,'cwd':str(self.root),'config':str(self.config),'account_id':'bob-agent','project_id':self.fx.pid,'development':False}
        receiver.ipc.write_receipt(self.folder/'binding.json',self.binding)
        incoming=self.fx.api('GET','/api/messages?for_me=1&unacked=1',who='bob-agent')['messages']
        if incoming:self.fx.api('POST','/api/receipts',{'kind':'ack','ids':[m['id'] for m in incoming]},who='bob-agent')
        self.calls=[]

    def event(self,kind,**payload):
        with self.journal.open('a') as stream:stream.write(json.dumps({'type':'event_msg','payload':dict(type=kind,**payload)})+'\n')

    def incoming(self,**kw):
        return self.fx.api('POST','/api/messages',{'body':'PEER_TEXT_DO_NOT_INJECT','to_ids':['bob-agent'],**kw},who='alice')['id']

    def accept(self,binding,text,qid,path):
        self.assertEqual(binding,self.binding);self.assertNotIn('PEER_TEXT_DO_NOT_INJECT',text)
        self.assertNotIn(self.fx.tokens['bob-agent'],text);self.assertIn('read',text);self.assertIn('observe',text)
        self.assertIn('отправлено: ',text);self.assertIn('доставлено клиенту: ',text)
        self.assertNotIn('доставлено клиенту: —',text)
        self.calls.append((qid,path,text))
        receipt={'status':'accepted','thread':self.session,'turn_id':'turn-'+str(len(self.calls)),'mode':'steer'}
        receiver.ipc.write_receipt(path,receipt);return receipt

    def step(self,deliver=None):
        original=receiver.tg.api_retry
        def api(conf,method,path,*args,**kwargs):
            if path.startswith('/api/wait'):return {'messages':[]}
            return original(conf,method,path,*args,**kwargs)
        with mock.patch.object(receiver.tg,'api_retry',side_effect=api):
            receiver.agent_step(self.conf,self.binding,self.folder,self.transcript,deliver or self.accept)

    def test_binds_only_the_explicit_workspace_account_and_project(self):
        with mock.patch.object(receiver.bridge,'session_transcript',return_value=self.journal),mock.patch.object(receiver.ipc,'check_owner'):
            self.assertEqual(receiver.bind(self.conf,self.config,self.session,self.root),self.binding)
            with self.assertRaises(RuntimeError):receiver.bind(self.conf,self.config,self.session,self.root/'other')
        for changed in ({'account_id':'alice-agent'},{'project_id':-1}):
            with self.assertRaises(RuntimeError):receiver.validate(self.conf,dict(self.binding,**changed))

    def test_notice_times_are_metadata_and_old_servers_do_not_invent_delivery(self):
        sent='2026-10-07T23:59:58+05:30'; received='2026-10-08T00:01:02+05:30'
        message={'id':42,'author_kind':'human','time':sent,'delivered_at':received}
        for render in (lambda m:receiver.notice([m],self.binding,self.config),
                       lambda m:receiver.bridge.notice(m,self.config,self.session),
                       receiver.tg.message_times):
            text=render(message);self.assertIn(sent,text);self.assertIn(received,text)
            self.assertIn('доставлено клиенту: —',render(dict(message,delivered_at=None)))
            injected=render(dict(message,time=sent+'\nPEER_TEXT_DO_NOT_INJECT',delivered_at='PEER_TEXT_DO_NOT_INJECT'))
            self.assertNotIn('PEER_TEXT_DO_NOT_INJECT',injected)
        receipt=receiver.tg.receipt_str({'user':'bob','delivered':sent,'read':received,'acked':None})
        self.assertIn('прочитано '+received,receipt)

    def test_restart_does_not_repeat_an_accepted_active_dispatch_or_ack_for_model(self):
        mid=self.incoming();self.step();self.assertEqual(len(self.calls),1)
        self.event('task_started',turn_id='turn-1')
        self.transcript=receiver.bridge.Transcript(self.journal);self.step();self.assertEqual(len(self.calls),1)
        msg=self.fx.api('GET',f'/api/messages/{mid}',who='bob-agent')['message']
        self.assertFalse(msg['read']);self.assertFalse(msg['acked'])

    def test_unobserved_aborted_turn_can_recover_but_observed_turn_never_replays(self):
        mid=self.incoming();self.step();self.event('task_started',turn_id='turn-1');self.event('turn_aborted',turn_id='turn-1')
        self.step();self.assertEqual(len(self.calls),2)
        path=self.folder/('message-'+str(mid)+'.json');record=receiver.ipc.load_json(path)
        self.assertEqual(record['attempt'],2)
        self.event('task_started',turn_id='turn-2');self.transcript.poll()
        with mock.patch.object(receiver.bridge,'session_transcript',return_value=self.journal):receiver.observe(self.conf,mid)
        self.event('task_complete',turn_id='turn-2',last_agent_message='done')
        self.step();self.assertEqual(len(self.calls),2)
        self.assertTrue(receiver.ipc.load_json(path)['observed_at'])

    def test_ambiguous_delivery_is_held_without_blind_replay(self):
        self.incoming()
        def uncertain(binding,text,key,path):
            receiver.ipc.write_receipt(path,{'status':'submitting','thread':self.session})
            raise receiver.ipc.RpcError('timeout')
        self.step(uncertain);self.step(lambda *a:self.fail('ambiguous request replayed'))
        self.assertEqual(receiver.ipc.load_json(self.folder/'status.json')['status'],'needs_attention')

    def test_failure_before_submission_keeps_same_attempt_and_id(self):
        self.incoming();keys=[]
        def unavailable(binding,text,key,path):keys.append(key);raise receiver.ipc.RpcError('no owner')
        for _ in range(5):self.step(unavailable)
        self.assertEqual(len(set(keys)),1)
        self.step();self.assertEqual(len(self.calls),1)

    def test_public_and_private_messages_do_not_enter_the_same_model_turn(self):
        self.incoming()
        cid=self.fx.api('POST','/api/conversations',{'peer_id':'bob-agent'},who='alice')['conversation']['id']
        private=self.incoming(conversation_id=cid)
        self.step();self.assertNotIn('read '+str(private),self.calls[0][2])
        self.event('task_started',turn_id='turn-1');self.step();self.assertEqual(len(self.calls),1)
        self.event('task_complete',turn_id='turn-1',last_agent_message='done');self.step()
        # The unread public item can retry; mark it read so its old notice no longer leads the batch.
        public=next(p for p in self.folder.glob('message-*.json') if receiver.ipc.load_json(p)['message_id']!=private)
        self.fx.api('GET',f"/api/messages/{receiver.ipc.load_json(public)['message_id']}?mark=read",who='bob-agent')
        self.event('task_started',turn_id='turn-2');self.event('task_complete',turn_id='turn-2',last_agent_message='done')
        self.step();self.assertIn(str(private),self.calls[-1][2])

    def test_service_waits_for_missing_owner_and_recovers_without_changing_binding(self):
        failures=[receiver.ipc.RpcError('offline'),None]
        def recovered(*args):
            self.assertEqual(args[1],self.binding);(self.folder/'stop').touch()
        with mock.patch.object(receiver.bridge,'session_transcript',return_value=self.journal),\
             mock.patch.object(receiver.bridge,'session_lock',return_value=nullcontext()),\
             mock.patch.object(receiver.ipc,'check_owner',side_effect=failures),\
             mock.patch.object(receiver,'agent_step',side_effect=recovered),mock.patch.object(receiver.time,'sleep'):
            receiver.service(self.conf)
        self.assertEqual(receiver.ipc.load_json(self.folder/'binding.json'),self.binding)

    def test_supervisor_restarts_a_crashed_worker(self):
        children=[]
        class Child:
            pid=123
            def wait(child):
                children.append(child)
                if len(children)==2:(self.folder/'stop').touch()
                return 1
        with mock.patch.object(receiver.bridge,'session_lock',return_value=nullcontext()),\
             mock.patch.object(receiver.subprocess,'Popen',side_effect=lambda *a,**kw:Child()),mock.patch.object(receiver.time,'sleep'):
            receiver.supervise(self.conf,self.config)
        self.assertEqual(len(children),2)


if __name__=='__main__':unittest.main(verbosity=2)
