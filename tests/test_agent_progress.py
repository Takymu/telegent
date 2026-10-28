"""Authenticated progress is separate from messages, scoped to its source and retry safe."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import uuid

import test_attachments as fixture
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'tools'))
import codex_chat_bridge as bridge


class ProgressTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest();cls.s=cls.fx.s

    @classmethod
    def tearDownClass(cls):fixture.AttachmentTest.tearDownClass()

    def source(self,**kw):return self.fx.api('POST','/api/messages',{'body':'task fixture',**kw},who='alice')['id']
    def post(self,mid,**kw):return self.fx.api('POST','/api/progress',{'message_id':mid,'body':'checking fixture','key':uuid.uuid4().hex,**kw})
    def events(self,agent='bob-agent',who='alice'):
        return self.fx.api('GET','/api/progress?project=main&agent='+agent,who=who)['events']
    def error(self,code,method,path,data=None,who='bob-agent'):
        with self.assertRaises(urllib.error.HTTPError) as caught:self.fx.api(method,path,data,who=who)
        self.assertEqual(caught.exception.code,code)

    def test_public_progress_uses_authenticated_agent_and_does_not_create_unread(self):
        mid=self.source();before=self.fx.api('GET','/api/messages',who='alice')['last_id']
        event=self.post(mid)
        self.assertEqual(self.events()[-1]['agent_id'],'bob-agent')
        self.assertEqual(self.events()[-1]['id'],event['id'])
        self.assertEqual(self.fx.api('GET','/api/messages',who='alice')['last_id'],before)
        self.error(403,'POST','/api/progress',{'message_id':mid,'body':'forge','key':'human'},who='alice')
        self.error(400,'POST','/api/progress',{'message_id':mid,'body':'forge','key':'actor','agent':'alice-agent'})

    def test_retry_validation_and_database_reopen(self):
        mid=self.source();key=uuid.uuid4().hex
        first=self.post(mid,key=key)
        self.assertEqual(self.post(mid,key=key),{'id':first['id'],'duplicate':True})
        self.error(409,'POST','/api/progress',{'message_id':mid,'body':'different','key':key})
        for data in ({'body':''},{'body':'x'*4001},{'message_id':True},{'state':'invented'},{'key':'space key'}):
            self.error(400,'POST','/api/progress',{'message_id':mid,'body':'fixture','key':uuid.uuid4().hex,**data})
        with self.s.DB_LOCK:self.s._db.close();self.s._db=None;self.s.db()
        self.assertTrue(any(e['id']==first['id'] for e in self.events()))

    def test_private_progress_never_opens_to_owner_other_agent_or_foreign_project(self):
        dm=self.fx.api('POST','/api/conversations',{'peer_id':'bob-agent'},who='alice')['conversation']['id']
        mid=self.source(conversation_id=dm);event=self.post(mid,body='private fixture')
        self.assertTrue(next(e for e in self.events() if e['id']==event['id'])['private'])
        self.assertNotIn(event['id'],[e['id'] for e in self.events(who='alice-agent')])
        self.error(404,'GET','/api/progress?project=main&agent=bob-agent',who='carol')
        self.error(404,'POST','/api/progress',{'message_id':mid,'body':'foreign','key':'private-forged'},who='alice-agent')
        self.error(404,'POST','/api/progress?project=other',{'message_id':mid,'body':'foreign','key':'wrong-project'})
        # Even the agent's human owner/admin must not see another human's private work.
        self.fx.tokens['bob']='tgt_'+self.s.secrets.token_hex(24)
        with self.s.DB_LOCK:
            self.s.db().execute('INSERT INTO users(name,kind,owner,token_hash,created_ts) VALUES (?,?,?,?,?)',
                                ('bob','human','bob',self.s.token_hash(self.fx.tokens['bob']),self.s.time.time()))
            self.s.add_member(self.s.db(),self.fx.pid,'bob','member','alice')
            self.s.db().execute("UPDATE users SET is_admin=1 WHERE name='alice'")
        private=self.fx.api('POST','/api/conversations',{'peer_id':'bob-agent'},who='bob')['conversation']['id']
        source=self.fx.api('POST','/api/messages',{'body':'colleague private fixture','conversation_id':private},who='bob')['id']
        step=self.post(source,body='colleague private progress')
        self.assertNotIn(step['id'],[e['id'] for e in self.events()])
        self.assertIn(step['id'],[e['id'] for e in self.events(who='bob')])
        with self.s.DB_LOCK:self.s.db().execute('DELETE FROM members WHERE project_id=? AND user=?',(self.fx.pid,'bob-agent'))
        try:self.error(404,'GET','/api/progress?project=main&agent=bob-agent',who='alice')
        finally:
            with self.s.DB_LOCK:self.s.add_member(self.s.db(),self.fx.pid,'bob-agent','agent','alice')

    def test_deleted_source_erases_progress_and_cli_preserves_dm_scope(self):
        mid=self.source();result=self.fx.cli('progress',str(mid),'CLI fixture','--state','waiting','--key',uuid.uuid4().hex)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(any(e['message_id']==mid and e['body']=='CLI fixture' for e in self.events()))
        self.fx.api('POST',f'/api/messages/{mid}/delete',{'revision':0},who='alice')
        self.assertFalse(any(e['message_id']==mid for e in self.events()))
        self.assertFalse(self.s.db().execute('SELECT 1 FROM agent_progress WHERE message_id=?',(mid,)).fetchone())


class ProgressBridgeTest(unittest.TestCase):
    def test_parser_only_accepts_user_facing_commentary(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'journal.jsonl'
            rows=[{'type':'event_msg','payload':{'type':'task_started','turn_id':'turn'}},
                  {'type':'response_item','payload':{'type':'reasoning','summary':'private fixture'}},
                  {'type':'response_item','payload':{'type':'message','role':'assistant','phase':'analysis','content':[{'type':'output_text','text':'private analysis'}]}},
                  {'type':'response_item','payload':{'type':'function_call','arguments':'private command'}},
                  {'type':'response_item','payload':{'type':'message','role':'assistant','phase':'commentary','content':[{'type':'output_text','text':'Checking the fixture'}]}}]
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            transcript=bridge.Transcript(path);transcript.poll()
            self.assertEqual([r[2] for r in transcript.updates],['Checking the fixture'])
            transcript.poll();self.assertEqual(len(transcript.updates),1)

    def test_opt_in_observation_scope_durable_retry_and_fixed_source(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);session=str(uuid.uuid4())
            conf=SimpleNamespace(share_progress=False)
            transcript=SimpleNamespace(updates=[('turn',5,'Before read'),('turn',50,'Public fixture')],completed={},closed=set())
            messages=[{'id':1,'project_id':2,'conversation_id':None}]
            bridge.ipc.write_receipt(folder/'message-1.json',{'thread':session,'observed_turn':'turn','observed_at':1,'observed_offset':10})
            with mock.patch.object(bridge.tg,'api_retry') as api:
                bridge.forward_progress(conf,session,transcript,folder,messages);api.assert_not_called()
                conf.share_progress=True;api.side_effect=bridge.tg.NetError('fixture offline')
                bridge.forward_progress(conf,session,transcript,folder,messages)
                ledger=bridge.ipc.load_json(folder/'progress-receipts.json');self.assertEqual(len(ledger['pending']),2)
                ledger.pop('retry_after');bridge.ipc.write_receipt(folder/'progress-receipts.json',ledger)
                api.side_effect=None;api.return_value={'agent_progress':True};api.reset_mock()
                # The source is already acknowledged; outbox still retries after restart.
                bridge.forward_progress(conf,session,transcript,folder,[])
                bodies=[c.args[3]['body'] for c in api.call_args_list if c.args[1]=='POST']
                self.assertIn('Public fixture',bodies);self.assertNotIn('Before read',bodies)
                self.assertFalse(bridge.ipc.load_json(folder/'progress-receipts.json')['pending'])
                api.reset_mock();bridge.forward_progress(conf,session,transcript,folder,messages);api.assert_not_called()
                messages.append({'id':2,'project_id':2,'conversation_id':'other'})
                bridge.ipc.write_receipt(folder/'message-2.json',{'thread':session,'observed_turn':'turn','observed_at':1,'observed_offset':10})
                transcript.updates.append(('turn',90,'Must not mix scopes'))
                bridge.forward_progress(conf,session,transcript,folder,messages);api.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
