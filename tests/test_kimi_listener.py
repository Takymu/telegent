"""Kimi ACP delivery, approval preservation and restart recovery."""
import io
import json
from pathlib import Path
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

    def test_failure_is_not_blindly_replayed(self):
        self.incoming()
        self.step(fail=True)
        self.step()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([x for x in self.calls if x[0] == 'prompt'], [])
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
