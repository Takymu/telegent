"""Native quota telemetry and a durable authenticated outbox, without model calls."""
import importlib.util
import concurrent.futures
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import time
import unittest
import uuid
from unittest import mock

import test_attachments as fixture

spec = importlib.util.spec_from_file_location('quota_tg', fixture.ROOT/'tg.py')
tg = importlib.util.module_from_spec(spec); spec.loader.exec_module(tg)
spec = importlib.util.spec_from_file_location('quota_kimi', fixture.ROOT/'tools/kimi_listener.py')
kimi = importlib.util.module_from_spec(spec); spec.loader.exec_module(kimi)


class QuotaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass(); cls.fx = fixture.AttachmentTest()

    @classmethod
    def tearDownClass(cls):
        fixture.AttachmentTest.tearDownClass()

    def setUp(self):
        self.root = self.fx.work/uuid.uuid4().hex; self.root.mkdir()
        self.config = self.root/'client.json'
        self.config.write_text(json.dumps({'server':self.fx.url, 'token':self.fx.tokens['bob-agent'],
            'url_source':'', 'auto_update':False, 'project':'main', 'state_file':str(self.root/'state.json')}))
        self.conf = tg.Conf(str(self.config))
        self.now = time.time(); self.reset = int(self.now + 86400)

    def snapshot(self, **limits):
        return {'type':'event_msg', 'payload':{'type':'token_count', 'rate_limits':limits}}

    def limited(self, provider='codex', reset=True):
        return {'provider':provider, 'state':'limited', 'reset_at':self.reset if reset else None, 'window':'недельный'}

    def notices(self):
        return [x for x in self.fx.api('GET','/api/messages?limit=1000',who='alice')['messages']
                if x['author_id'] == 'bob-agent' and x['body'].startswith('Агент исчерпал лимит')]

    def test_codex_windows_credits_and_nonquota_errors(self):
        event = self.snapshot(primary={'used_percent':100,'window_minutes':300,'resets_at':self.reset-500},
                              secondary={'used_percent':100,'window_minutes':10080,'resets_at':self.reset})
        signal = tg.quota_signal('codex', event, self.now)
        self.assertEqual(signal['reset_at'], self.reset); self.assertEqual(signal['window'],'недельный')
        event['payload']['rate_limits']['credits'] = {'has_credits':True}
        self.assertIsNone(tg.quota_signal('codex', event, self.now))
        self.assertIsNone(tg.quota_error('codex', {'code':429,'message':'rate limit reached; tokens per minute'}))
        self.assertIsNone(tg.quota_signal('codex', {'type':'response_item','payload':{'type':'message',
            'role':'user','content':[{'text':"You've hit your usage limit"}]}}))
        self.assertIsNotNone(tg.quota_signal('codex', {'type':'event_msg','payload':{
            'type':'error','codex_error_info':{'usageLimitExceeded':{}}}}))
        self.assertIsNone(tg.quota_signal('codex', self.snapshot(primary={
            'used_percent':100,'resets_at':self.now-1}), self.now))

    def test_claude_native_rejected_and_synthetic_error_only(self):
        event = {'type':'rate_limit_event','rate_limit_info':{'status':'rejected',
            'rateLimitType':'five_hour','resetsAt':self.reset}}
        self.assertEqual(tg.quota_signal('claude',event)['reset_at'],self.reset)
        event['rate_limit_info'] = {'overageStatus':'rejected'}
        self.assertIsNone(tg.quota_signal('claude',event))
        event = {'type':'assistant','message':{'model':'example-model','content':[
            {'type':'text','text':"You've hit your limit"}]}}
        self.assertEqual(tg.quota_signal('claude',event)['state'],'ok')
        event['isApiErrorMessage'] = True
        self.assertEqual(tg.quota_signal('claude',event)['state'],'limited')
        self.assertIsNone(tg.quota_signal('claude', {'hook_event_name':'StopFailure',
            'error':'rate_limit','last_assistant_message':'Server is temporarily limiting requests (not your usage limit)'}))
        self.assertIsNone(tg.quota_signal('claude', {'hook_event_name':'StopFailure','error':'max_output_tokens'}))

    def test_kimi_rpc_and_swallowed_native_error(self):
        error = {'code':-32603,'message':'Internal error','data':{'error':
            "403 You've reached your usage limit for this billing cycle. Your quota will be refreshed in the next cycle."}}
        self.assertIsNone(tg.quota_error('kimi',{'message':'429 rate_limit_reached_error'}))
        self.assertIsNone(tg.quota_error('kimi',{'message':'403 forbidden'}))
        self.assertIsNone(tg.quota_error('kimi',error)['reset_at'])
        dated = dict(error, data={'error':"You've reached your usage limit; resets at 2099-12-01T00:00:00Z"})
        self.assertGreater(tg.quota_error('kimi',dated)['reset_at'],self.now)
        client = kimi.Acp({'thread':'example-session'})
        client._read_stderr(io.StringIO('tool output: usage limit exceeded\n'
            'llm request failed errorName=APIStatusError errorMessage="403 You\'ve reached your usage limit for this billing cycle." statusCode=403\n'))
        self.assertEqual(client.quota_signals.qsize(),1)
        self.assertEqual(client.quota_signals.get()['provider'],'kimi')
        client.messages.put({'jsonrpc':'2.0','id':1,'error':error})
        with self.assertRaises(kimi.AcpError): client.wait_response(1,1)
        self.assertEqual(client.quota_signals.qsize(),1)

    def test_real_author_scope_dedup_and_lost_response_retry(self):
        before = len(self.notices())
        tg.quota_record(self.conf,self.limited())
        real = tg.api
        def lost(conf, method, path, *args, **kw):
            result = real(conf,method,path,*args,**kw)
            if method == 'POST': raise tg.NetError('response lost after commit')
            return result
        with mock.patch.object(tg,'api',side_effect=lost):
            with self.assertRaises(tg.NetError): tg.quota_flush(self.conf)
        fresh = tg.Conf(str(self.config))
        tg.quota_flush(fresh)
        tg.quota_record(fresh,self.limited()); tg.quota_flush(fresh)
        rows = self.notices(); self.assertEqual(len(rows),before+1)
        notice = rows[-1]
        self.assertEqual(notice['project_id'],self.fx.pid)
        self.assertIsNone(notice.get('conversation_id'))
        self.assertEqual(notice['author_kind'],'agent'); self.assertIn('UTC',notice['body'])
        self.assertEqual(self.fx.api('GET','/api/messages',who='carol')['messages'],[])

    def test_pending_recovery_identity_change_and_disabled(self):
        before = len(self.notices())
        tg.quota_record(self.conf,self.limited(reset=False))
        with mock.patch.object(tg,'api',side_effect=tg.NetError('offline')):
            with self.assertRaises(tg.NetError): tg.quota_flush(self.conf)
        tg.quota_record(self.conf,{'provider':'codex','state':'ok'})
        tg.quota_flush(self.conf); self.assertEqual(len(self.notices()),before)
        tg.quota_record(self.conf,self.limited(reset=False))
        self.conf.token = self.fx.tokens['alice-agent']
        tg.quota_flush(self.conf); self.assertEqual(len(self.notices()),before)
        self.conf.token = self.fx.tokens['bob-agent']
        tg.quota_flush(self.conf)
        self.assertIn('сервис не сообщил время',self.notices()[-1]['body'])
        tg.quota_record(self.conf,self.limited(reset=False)); tg.quota_flush(self.conf)
        self.assertEqual(len(self.notices()),before+1)
        self.conf.quota_notifications = False
        tg.quota_record(self.conf,self.limited('kimi')); tg.quota_flush(self.conf)
        self.assertEqual(len(self.notices()),before+1)

    def test_human_token_cannot_automatically_publish_quota(self):
        self.conf.token = self.fx.tokens['alice']
        tg.quota_record(self.conf,self.limited())
        with self.assertRaises(ValueError): tg.quota_flush(self.conf)

    def test_secretary_reports_availability_only_in_current_memberships(self):
        title = 'quota-' + uuid.uuid4().hex[:8]
        project = self.fx.api('POST','/api/projects',{'title':title,'slug':title},who='alice')['project']
        invite = self.fx.api('POST','/api/invites',{'name':'helper-'+uuid.uuid4().hex[:8],
            'kind':'agent','is_secretary':True},who='alice')
        joined = self.fx.api('POST','/api/join',{'code':invite['code']},who=None)
        self.fx.tokens[joined['id']] = joined['token']
        self.conf.token = joined['token']; self.conf.project = None
        try:
            tg.quota_record(self.conf,self.limited()); tg.quota_flush(self.conf); tg.quota_flush(self.conf)
            for slug in ('main',project['slug']):
                rows = self.fx.api('GET','/api/messages?project='+slug,who='alice')['messages']
                notices = [m for m in rows if m['author_id'] == joined['id']]
                self.assertEqual(len(notices),1)
            self.assertFalse(any(m['author_id'] == joined['id'] for m in self.fx.api(
                'GET','/api/messages',who='carol')['messages']))
        finally:
            self.fx.api('POST','/api/secretaries/revoke',{'account_id':joined['id']},who='alice')
            self.fx.api('POST','/api/project/delete?project='+project['slug'],{'confirm':project['slug']},who='alice')

    def test_two_offline_episodes_are_not_lost_and_known_expired_not_sent(self):
        before = len(self.notices())
        tg.quota_record(self.conf,self.limited())
        second = self.limited(); second['reset_at'] += 100
        tg.quota_record(self.conf,second); tg.quota_flush(self.conf)
        self.assertEqual(len(self.notices()),before+2)
        tg.quota_record(self.conf,self.limited('kimi'))
        with mock.patch.object(tg.time,'time',return_value=self.reset+1): tg.quota_flush(self.conf)
        self.assertEqual(len(self.notices()),before+2)

    def test_parallel_observers_keep_one_notice(self):
        before = len(self.notices())
        def observe(_):
            conf = tg.Conf(str(self.config))
            tg.quota_record(conf,self.limited()); tg.quota_flush(conf)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(observe,range(6)))
        self.assertEqual(len(self.notices()),before+1)

    def test_native_cursor_partial_line_restart_and_no_history_replay(self):
        session = str(uuid.uuid4()); journal = self.root/(session+'.jsonl')
        journal.write_text(json.dumps(self.snapshot(primary={'used_percent':100,'resets_at':self.reset}))+'\n')
        event = {'session_id':session,'transcript_path':str(journal),'cwd':os.getcwd()}
        with mock.patch.object(tg.subprocess,'Popen') as launch:
            tg.quota_bind(self.conf,'codex',event); self.assertEqual(launch.call_count,1)
        path = next(self.root.glob('state.json.quota-session-*.json'))
        before = len(self.notices()); tg.quota_poll(self.conf,str(path))
        self.assertEqual(len(self.notices()),before)
        raw = json.dumps(self.snapshot(primary={'used_percent':100,'window_minutes':300,'resets_at':self.reset+100})).encode()
        with journal.open('ab') as f: f.write(raw)
        tg.quota_poll(self.conf,str(path)); self.assertEqual(len(self.notices()),before)
        with journal.open('ab') as f: f.write(b'\n')
        tg.quota_poll(self.conf,str(path)); tg.quota_poll(tg.Conf(str(self.config)),str(path))
        self.assertEqual(len(self.notices()),before+1)
        event['cwd'] = str(self.root)
        with mock.patch.object(tg.subprocess,'Popen') as launch:
            tg.quota_bind(self.conf,'codex',event); launch.assert_not_called()

    def test_actual_background_monitor_without_model_turn(self):
        session = str(uuid.uuid4()); journal = self.root/(session+'.jsonl'); journal.write_text('')
        before = len(self.notices())
        launched = []
        actual_popen = subprocess.Popen
        def launch(*args, **kwargs):
            proc = actual_popen(*args, **kwargs); launched.append(proc); return proc
        with mock.patch.dict(os.environ,self.fx.env,clear=True), mock.patch.object(tg.subprocess,'Popen',side_effect=launch):
            tg.quota_bind(self.conf,'claude',{'session_id':session,'transcript_path':str(journal),'cwd':os.getcwd()})
        self.assertEqual(len(launched),1); proc = launched[0]
        try:
            event = {'type':'rate_limit_event','rate_limit_info':{'status':'rejected',
                'rateLimitType':'five_hour','resetsAt':self.reset+200}}
            with journal.open('a') as f: f.write(json.dumps(event)+'\n')
            until = time.time()+12
            while len(self.notices()) == before and time.time() < until:
                self.assertIsNone(proc.poll()); time.sleep(.2)
            self.assertEqual(len(self.notices()),before+1)
        finally:
            proc.terminate(); proc.wait(timeout=5)


if __name__ == '__main__': unittest.main()
