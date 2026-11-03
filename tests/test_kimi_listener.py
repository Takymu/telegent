"""Kimi ACP delivery, default tool approvals and restart recovery."""
import io
import queue
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
import uuid
from unittest import mock

import test_attachments as fixture

spec = fixture.importlib.util.spec_from_file_location('test_kimi_receiver', fixture.ROOT / 'tools/kimi_listener.py')
receiver = fixture.importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


class FakeAcp:
    def __init__(self, binding, calls, fail=False):
        self.binding = binding
        self.calls = calls
        self.fail = fail

    def start(self):
        self.calls.append(('start', self.binding['thread']))
        if self.fail:
            raise receiver.AcpError('unavailable')

    def prompt(self, text):
        self.calls.append(('prompt', text))

    def close(self):
        self.calls.append(('close', self.binding['thread']))


class ReceiverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass()
        cls.fx = fixture.AttachmentTest()

    @classmethod
    def tearDownClass(cls):
        fixture.AttachmentTest.tearDownClass()

    def setUp(self):
        self.root = self.fx.work / uuid.uuid4().hex
        self.root.mkdir()
        self.config = self.root / 'client.json'
        self.config.write_text(json.dumps({
            'server': self.fx.url, 'token': self.fx.tokens['bob-agent'], 'project': 'main',
            'url_source': '', 'auto_update': False, 'state_file': str(self.root / 'state.json'),
        }))
        self.conf = receiver.tg.Conf(str(self.config))
        self.folder = receiver.runtime(self.conf)
        self.session = 'kimi-session-' + uuid.uuid4().hex
        self.binding = {
            'thread': self.session, 'cwd': str(self.root), 'config': str(self.config),
            'account_id': 'bob-agent', 'project_id': self.fx.pid, 'kimi_cli': 'kimi',
        }
        receiver.write_receipt(self.folder / 'binding.json', self.binding)
        incoming = self.fx.api('GET', '/api/messages?for_me=1&unacked=1', who='bob-agent')['messages']
        if incoming:
            self.fx.api('POST', '/api/receipts', {'kind': 'ack', 'ids': [m['id'] for m in incoming]}, who='bob-agent')
        self.calls = []

    def incoming(self, **kw):
        return self.fx.api('POST', '/api/messages', {
            'body': 'PEER_TEXT_DO_NOT_INJECT', 'to_ids': ['bob-agent'], **kw,
        }, who='alice')['id']

    def step(self, fail=False):
        original = receiver.tg.api_retry

        def api(conf, method, path, *args, **kwargs):
            if path.startswith('/api/wait'):
                return {'messages': []}
            return original(conf, method, path, *args, **kwargs)

        with mock.patch.object(receiver.tg, 'api_retry', side_effect=api):
            receiver.agent_step(self.conf, self.binding, self.folder,
                                acp_factory=lambda binding: FakeAcp(binding, self.calls, fail))

    def test_bind_checks_real_acp_and_preserves_kimi_hint(self):
        with mock.patch.object(receiver, 'Acp', lambda binding: FakeAcp(binding, self.calls)):
            result = receiver.bind(self.conf, self.config, self.session, self.root)
        self.assertEqual(result['thread'], self.session)
        self.assertEqual(result['kimi_cli'], 'kimi')
        self.assertEqual(self.conf.agent_client, 'kimi')
        self.assertEqual(self.calls[0], ('start', self.session))
        self.assertEqual(self.calls[1], ('close', self.session))

    def test_notice_has_only_commands_and_metadata(self):
        mid = self.incoming()
        text = receiver.notice([{'id': mid, 'author_kind': 'human'}], self.binding, self.config)
        self.assertNotIn('PEER_TEXT_DO_NOT_INJECT', text)
        self.assertNotIn(self.fx.tokens['bob-agent'], text)
        self.assertIn('read', text)
        self.assertNotIn('kimi -p', text)
        self.assertNotIn('--auto', text)

    def test_native_quota_posts_as_agent_without_observing_incoming(self):
        mid = self.incoming()
        outer = self
        class QuotaAcp(FakeAcp):
            def __init__(self, binding):
                super().__init__(binding, outer.calls)
                self.quota_signals = queue.Queue()
            def prompt(self, text):
                self.quota_signals.put({'provider':'kimi','state':'limited','reset_at':None,'window':''})
                raise receiver.AcpError('PRIVATE_PROVIDER_ERROR_DO_NOT_EXPORT')
        original = receiver.tg.api_retry
        def api(conf, method, path, *args, **kwargs):
            return {'messages':[]} if path.startswith('/api/wait') else original(conf,method,path,*args,**kwargs)
        with mock.patch.object(receiver.tg,'api_retry',side_effect=api):
            receiver.agent_step(self.conf,self.binding,self.folder,acp_factory=QuotaAcp)
        messages = self.fx.api('GET','/api/messages?limit=1000',who='alice')['messages']
        notice = next(m for m in reversed(messages) if 'исчерпал лимит использования Kimi Code' in m['body'])
        self.assertEqual(notice['author_id'],'bob-agent')
        self.assertNotIn('PRIVATE_PROVIDER_ERROR',notice['body'])
        self.assertFalse(receiver.load_json(self.folder/('message-'+str(mid)+'.json')).get('observed_at'))
        incoming = self.fx.api('GET',f'/api/messages/{mid}',who='bob-agent')['message']
        self.assertFalse(incoming['read']); self.assertFalse(incoming['acked'])

    def test_success_marks_observed_once_and_never_replays(self):
        mid = self.incoming()
        self.step()
        self.assertEqual(len([x for x in self.calls if x[0] == 'prompt']), 1)
        path = self.folder / ('message-' + str(mid) + '.json')
        self.assertTrue(receiver.load_json(path)['observed_at'])
        self.step()
        self.assertEqual(len([x for x in self.calls if x[0] == 'prompt']), 1)
        msg = self.fx.api('GET', f'/api/messages/{mid}', who='bob-agent')['message']
        self.assertFalse(msg['read'])
        self.assertFalse(msg['acked'])

    def test_submitting_or_accepted_without_observation_is_held(self):
        mid = self.incoming()
        path = self.folder / ('message-' + str(mid) + '.json')
        for status in ('submitting', 'accepted'):
            receiver.write_receipt(path, {
                'thread': self.session, 'message_id': mid, 'dispatch': 'dispatch-test.json', 'attempt': 1,
            })
            receiver.write_receipt(self.folder / 'dispatch-test.json', {'status': status})
            self.step()
            self.assertEqual(self.calls, [])
            self.assertEqual(receiver.load_json(self.folder / 'status.json')['status'], 'needs_attention')

    def test_connect_failure_recovers_before_any_prompt_was_sent(self):
        self.incoming()
        self.step(fail=True)
        self.step()
        self.assertEqual(len([x for x in self.calls if x[0] == 'start']), 2)
        self.assertEqual(len([x for x in self.calls if x[0] == 'prompt']), 1)
        self.assertEqual(receiver.load_json(self.folder / 'status.json')['status'], 'listening')

    def test_ambiguous_prompt_failure_is_not_replayed(self):
        mid = self.incoming()
        with mock.patch.object(FakeAcp, 'prompt', side_effect=receiver.AcpError('connection lost')):
            self.step()
        self.step()
        self.assertEqual(len([x for x in self.calls if x[0] == 'start']), 1)
        self.assertFalse(receiver.load_json(self.folder / ('message-' + str(mid) + '.json')).get('observed_at'))
        self.assertEqual(receiver.load_json(self.folder / 'status.json')['status'], 'needs_attention')

    def test_public_and_private_messages_use_separate_turns(self):
        public = self.incoming()
        cid = self.fx.api('POST', '/api/conversations', {'peer_id': 'bob-agent'}, who='alice')['conversation']['id']
        private = self.incoming(conversation_id=cid)
        self.step()
        text = next(x[1] for x in self.calls if x[0] == 'prompt')
        self.assertIn('#' + str(public), text)
        self.assertNotIn('#' + str(private), text)
        self.step()
        text = next(x[1] for x in self.calls if x[0] == 'prompt' and '#' + str(private) in x[1])
        self.assertIn('#' + str(private), text)

    def test_reminder_reaches_kimi_and_does_not_spam(self):
        self.conf.remind_after_minutes = 0.001
        self.conf.remind_interval_minutes = 60
        mid = self.incoming(type='question')
        # Server ages are rounded to seconds; use an actually overdue fixture.
        self.fx.s.db().execute('UPDATE messages SET ts=ts-120 WHERE id=?', (mid,))
        self.step()
        reminders = [x for x in self.calls if x[0] == 'prompt' and 'напоминание:' in x[1]]
        self.assertEqual(len(reminders), 1)
        self.step()
        reminders = [x for x in self.calls if x[0] == 'prompt' and 'напоминание:' in x[1]]
        self.assertEqual(len(reminders), 1)

    def test_reminder_can_be_disabled(self):
        self.conf.remind_after_minutes = 0
        self.incoming()
        self.step()
        self.assertEqual([x for x in self.calls if x[0] == 'prompt' and 'напоминание:' in x[1]], [])

    def test_acp_start_uses_initialize_and_session_resume(self):
        replies = [
            {'jsonrpc': '2.0', 'id': 1, 'result': {'agentInfo': {'name': 'Kimi Code CLI'}}},
            {'jsonrpc': '2.0', 'id': 2, 'result': {}},
        ]

        class Proc:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = iter([json.dumps(x) + '\n' for x in replies])
                self.stderr = iter([])

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        proc = Proc()
        with mock.patch.object(receiver.subprocess, 'Popen', return_value=proc):
            client = receiver.Acp(self.binding)
            client.start()
        methods = [json.loads(line)['method'] for line in proc.stdin.getvalue().splitlines()]
        self.assertEqual(methods, ['initialize', 'session/resume'])
        client.close()

    def test_vscode_session_lock_has_actionable_russian_error(self):
        error = receiver.AcpError('Kimi ACP request failed', {
            'code': -32603, 'message': 'Internal error',
            'data': {'details': 'This session is in use by another Kimi Code for VS Code instance.'},
        })
        text = receiver.acp_error_text(error)
        self.assertIn('VS Code', text)
        self.assertIn('отдельную CLI-сессию', text)
        self.assertNotIn('Internal error', text)

    def test_permission_notification_is_not_auto_answered(self):
        binding = dict(self.binding)
        client = receiver.Acp(binding)
        client.proc = types.SimpleNamespace(stdin=io.StringIO())
        permission = {'jsonrpc': '2.0', 'method': 'session/request_permission', 'params': {'reason': 'edit'}}
        response = {'jsonrpc': '2.0', 'id': 1, 'result': {'ok': True}}
        client.messages.put(permission)
        client.messages.put(response)
        result = client.request('session/prompt', {'prompt': []})
        self.assertEqual(result, {'ok': True})
        self.assertEqual(client.messages.qsize(), 0)

    def permission_client(self, session=None, options=None):
        client = receiver.Acp(dict(self.binding))
        client.proc = types.SimpleNamespace(stdin=io.StringIO())
        client.messages.put({
            'jsonrpc': '2.0', 'id': 1, 'method': 'session/request_permission',
            'params': {'sessionId': session or self.session, 'options': options or [
                {'optionId': 'persist', 'kind': 'allow_always'},
                {'optionId': 'deny', 'kind': 'reject_once'},
                {'optionId': 'run', 'kind': 'allow_once'},
            ]},
        })
        client.messages.put({'jsonrpc': '2.0', 'id': 1, 'result': {'stopReason': 'end_turn'}})
        return client

    def test_tool_approval_is_automatic_by_default_despite_colliding_rpc_ids(self):
        client = self.permission_client()
        self.assertEqual(client.prompt('notice'), {'stopReason': 'end_turn'})
        written = [json.loads(line) for line in client.proc.stdin.getvalue().splitlines()]
        self.assertEqual(len(written), 2)
        self.assertEqual(written[1], {'jsonrpc': '2.0', 'id': 1, 'result': {
            'outcome': {'outcome': 'selected', 'optionId': 'run'},
        }})
        self.assertNotIn('auto_approve', self.binding)

    def test_foreign_session_permissions_are_cancelled(self):
        client = self.permission_client(session='another-session')
        with self.assertRaises(receiver.PermissionRequired):
            client.prompt('notice')
        written = [json.loads(line) for line in client.proc.stdin.getvalue().splitlines()]
        self.assertEqual(written[1]['result']['outcome'], {'outcome': 'cancelled'})

    def test_unrecognized_permission_options_are_cancelled_without_hanging(self):
        client = self.permission_client(options=[{'optionId': 'persist', 'kind': 'allow_always'}])
        with self.assertRaises(receiver.PermissionRequired):
            client.prompt('notice')

    def test_old_lock_file_does_not_block_recovery_and_live_lock_excludes_duplicate(self):
        key = receiver.supervisor_key(self.binding)
        Path(receiver.runtime_lock(key)).touch()
        self.assertTrue(receiver.unlocked(key))
        with receiver.session_lock(key):
            self.assertFalse(receiver.unlocked(key))
        self.assertTrue(receiver.unlocked(key))

    def test_process_crash_releases_supervisor_lock(self):
        key = receiver.supervisor_key(self.binding)
        script = (
            'import importlib.util,sys,time\n'
            's=importlib.util.spec_from_file_location("receiver",sys.argv[1])\n'
            'r=importlib.util.module_from_spec(s);s.loader.exec_module(r)\n'
            'with r.session_lock(sys.argv[2]):\n'
            ' print("locked",flush=True)\n'
            ' time.sleep(60)\n'
        )
        child = subprocess.Popen([sys.executable, '-u', '-c', script, receiver.__file__, key],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            self.assertFalse(receiver.unlocked(key))
            child.kill()
            child.communicate(timeout=5)
            self.assertTrue(receiver.unlocked(key))
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_real_pipe_protocol_runs_only_after_automatic_permission_response(self):
        script = self.root / 'acp_fixture.py'
        script.write_text('''import json, sys
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request['method'] != 'session/prompt':
        send({'jsonrpc': '2.0', 'id': request['id'], 'result': {}})
        continue
    send({'jsonrpc': '2.0', 'id': request['id'], 'method': 'session/request_permission',
          'params': {'sessionId': request['params']['sessionId'], 'options': [
              {'optionId': 'run', 'kind': 'allow_once'}]}})
    response = json.loads(sys.stdin.readline())
    assert response['result']['outcome'] == {'outcome': 'selected', 'optionId': 'run'}
    send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {}})
    send({'jsonrpc': '2.0', 'id': request['id'], 'result': {'stopReason': 'end_turn'}})
''', encoding='utf-8')
        real_popen = subprocess.Popen
        def start_fixture(argv, **kwargs):
            self.assertEqual(argv, ['kimi', 'acp'])
            return real_popen([sys.executable, '-u', str(script)], **kwargs)
        client = receiver.Acp(self.binding, prompt_timeout=5)
        try:
            with mock.patch.object(receiver.subprocess, 'Popen', side_effect=start_fixture):
                client.start()
                self.assertEqual(client.prompt('notice'), {'stopReason': 'end_turn'})
        finally:
            client.close()

    def test_startup_flag_is_accepted_and_forwarded_without_extra_modes(self):
        (self.folder / 'stop').touch()
        fake_child = types.SimpleNamespace(pid=123)
        with mock.patch.object(sys, 'argv', ['kimi_listener.py', '--config', str(self.config), 'start', '--startup']), \
             mock.patch.object(receiver, 'startup') as startup, \
             mock.patch.object(receiver.subprocess, 'Popen', return_value=fake_child):
            receiver.main()
        self.assertEqual(startup.call_args.args[1:], (str(self.config), True))

    def test_supervisor_restarts_crashed_worker(self):
        children = []

        class Child:
            pid = 321

            def wait(child):
                children.append(child)
                if len(children) == 2:
                    (self.folder / 'stop').touch()
                return 1

        with mock.patch.object(receiver.subprocess, 'Popen', side_effect=lambda *a, **kw: Child()), \
             mock.patch.object(receiver.time, 'sleep'):
            receiver.supervise(self.conf, self.config)
        self.assertEqual(len(children), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
