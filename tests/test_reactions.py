"""Reactions and member agent onboarding against a real isolated server."""
import concurrent.futures
from contextlib import closing
import subprocess
import time
import unittest
import urllib.error

import test_codex as fixture

DELETE_PHRASE = 'Да, я хочу удалить проект и чат вместе с ним'


class ReactionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.CodexTest.setUpClass()
        cls.fx = fixture.CodexTest('test_metadata_and_receipts')
        cls.project = cls.fx.api('GET', '/api/me')['project']['slug']
        invite = cls.fx.api('POST', '/api/invites', {'name': 'bob', 'kind': 'human'}, 'alice')
        joined = cls.fx.api('POST', '/api/join', {'code': invite['code'], 'password': 'bob fixture password'})
        cls.fx.tokens['bob'] = joined['token']

    @classmethod
    def tearDownClass(cls):
        fixture.CodexTest.tearDownClass()

    def setUp(self):
        self.fx.setUp()
        self.mid = self.fx.send(type='info', priority='normal')

    def api(self, method, path, data=None, author='bob-agent', project=None):
        path += ('&' if '?' in path else '?') + 'project=' + (project or self.project)
        return self.fx.api(method, path, data, author)

    def react(self, emoji, active=True, author='bob-agent', mid=None):
        return self.api('POST', f'/api/messages/{mid or self.mid}/reaction', {'emoji': emoji, 'active': active}, author)

    def test_reactions_are_idempotent_and_personal(self):
        for emoji in ('❤️', '🔥', '👍', '👎', '+1', '🤡'):
            self.react(emoji)
            self.react(emoji)
        self.react('❤', author='alice')
        msg = self.api('GET', f'/api/messages/{self.mid}')['message']
        self.assertEqual(len(msg['reactions']), 6)
        self.assertEqual(msg['reactions'][0], {'emoji': '❤️', 'count': 2, 'mine': True, 'users': ['bob-agent', 'alice']})
        self.assertFalse(msg['read']); self.assertFalse(msg['acked'])
        self.assertFalse(msg['approvals'])
        self.assertFalse(msg['needs_approval'])
        self.react('❤️', False)
        self.react('❤️', False)
        heart = self.api('GET', f'/api/messages/{self.mid}')['message']['reactions'][0]
        self.assertEqual(heart, {'emoji': '❤️', 'count': 1, 'mine': False, 'users': ['alice']})
        listed = self.api('GET', f'/api/messages?after={self.mid-1}&full=0')['messages'][0]
        self.assertEqual(listed['reactions'][0], heart)

    def test_validation_and_project_isolation(self):
        for data in ({'emoji': '💩'}, {'emoji': '🔥', 'active': 'false'}, {'emoji': '🔥', 'active': 0}, []):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.api('POST', f'/api/messages/{self.mid}/reaction', data)
            self.assertEqual(error.exception.code, 400)
        slug = self.api('POST', '/api/projects', {'title': 'Other team'}, 'alice')['project']['slug']
        try:
            other = self.api('POST', '/api/messages', {'body': 'private fixture'}, 'alice', slug)['id']
            revision = self.api('GET', '/api/messages')['reaction_revision']
            self.api('POST', f'/api/messages/{other}/reaction', {'emoji': '🔥'}, 'alice', slug)
            self.assertEqual(self.api('GET', '/api/messages')['reaction_revision'], revision)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.react('🤡', mid=other)
            self.assertEqual(error.exception.code, 404)
        finally:
            self.api('POST', '/api/project/delete', {'confirm': DELETE_PHRASE}, 'alice', slug)
        with closing(fixture.sqlite3.connect(fixture.CodexTest.db)) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM reactions WHERE message_id=?', (other,)).fetchone()[0], 0)

    def test_reactions_wake_browser_poll_without_creating_messages(self):
        before = self.api('GET', '/api/messages')
        path = f"/api/wait?after={before['last_id']}&for_me=0&timeout=2&reaction_revision={before['reaction_revision']}"
        with concurrent.futures.ThreadPoolExecutor() as pool:
            started = time.monotonic()
            pending = pool.submit(self.api, 'GET', path)
            time.sleep(.1)
            self.react('+1', author='alice')
            result = pending.result(timeout=3)
        self.assertLess(time.monotonic()-started, 1.7)
        self.assertEqual(result['messages'], [])
        self.assertEqual(result['last_id'], before['last_id'])
        self.assertNotEqual(result['reaction_revision'], before['reaction_revision'])
        revision = self.api('GET', '/api/messages')['reaction_revision']
        self.react('+1', author='alice')
        self.assertEqual(self.api('GET', '/api/messages')['reaction_revision'], revision)

    def test_agent_cli_and_rename_preserve_reactions(self):
        result = self.fx.cli('react', str(self.mid), '🤡')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('реакция 🤡', self.fx.cli('read', str(self.mid), '--no-mark').stdout)
        self.react('🔥', author='alice-agent')
        self.api('POST', '/api/rename', {'name': 'alice-agent', 'new': 'alice-helper'}, 'alice')
        try:
            fire = next(r for r in self.api('GET', f'/api/messages/{self.mid}')['message']['reactions'] if r['emoji'] == '🔥')
            self.assertEqual(fire['users'], ['alice-helper'])
        finally:
            self.api('POST', '/api/rename', {'name': 'alice-helper', 'new': 'alice-agent'}, 'alice')
        self.assertEqual(self.fx.cli('react', str(self.mid), '🤡', '--remove').returncode, 0)

    def test_reactions_survive_server_restart(self):
        self.react('👍')
        old = fixture.CodexTest.server
        old.terminate(); old.wait(timeout=5)
        fixture.CodexTest.server = subprocess.Popen(old.args, env=fixture.ENV,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                message = self.api('GET', f'/api/messages/{self.mid}')['message']
                break
            except OSError:
                time.sleep(.1)
        else:
            self.fail('server did not restart')
        self.assertEqual(message['reactions'][0]['emoji'], '👍')
        self.assertTrue(message['reactions'][0]['mine'])

    def test_member_can_invite_and_add_own_agents(self):
        invite = self.api('POST', '/api/invites', {'name': 'bob-helper', 'kind': 'agent'}, 'bob')
        self.assertEqual(invite['owner'], 'bob')
        joined = self.api('POST', '/api/join', {'code': invite['code']})
        self.fx.tokens['bob-helper'] = joined['token']
        me = self.api('GET', '/api/me', author='bob-helper')
        self.assertEqual(me['project']['slug'], self.project)
        self.assertEqual(me['owner'], 'bob')
        self.api('POST', '/api/members/remove', {'name': 'bob-helper'}, 'alice')
        candidates = self.api('GET', '/api/members/candidates', author='bob')['candidates']
        self.assertIn('bob-helper', [c['name'] for c in candidates])
        self.assertTrue(all(c['kind'] == 'agent' for c in candidates))
        self.api('POST', '/api/members/add', {'name': 'bob-helper'}, 'bob')
        self.assertEqual(self.api('GET', '/api/me', author='bob-helper')['project']['slug'], self.project)

    def test_member_cannot_invite_people_or_take_others_agents(self):
        for payload in ({'name': 'carol', 'kind': 'human'},
                        {'name': 'carol-agent', 'kind': 'agent', 'owner': 'alice'},
                        {'name': 'alice', 'kind': 'agent'},
                        {'name': 'alice-agent', 'kind': 'agent'}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.api('POST', '/api/invites', payload, 'bob')
            self.assertEqual(error.exception.code, 403)
        self.api('POST', '/api/invites', {'name': 'reserved-agent', 'kind': 'agent'}, 'alice')
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.api('POST', '/api/invites', {'name': 'reserved-agent', 'kind': 'agent'}, 'bob')
        self.assertEqual(error.exception.code, 409)
        self.assertFalse(any(i['name'] == 'reserved-agent' for i in self.api('GET', '/api/invites', author='bob')['invites']))
        self.assertEqual(self.api('POST', '/api/invites/cancel', {'name': 'reserved-agent'}, 'bob')['cancelled'], 0)
        for method, path in (('POST', '/api/invites'), ('POST', '/api/members/add'), ('GET', '/api/invites'),
                             ('POST', '/api/invites/cancel'), ('GET', '/api/members/candidates')):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.api(method, path, {'name': 'new-agent', 'kind': 'agent'} if method == 'POST' else None)
            self.assertEqual(error.exception.code, 403)

    def test_member_manages_own_invites_and_leaving_invalidates_them(self):
        invite = self.api('POST', '/api/invites', {'name': 'bob-cancel-agent', 'kind': 'agent'}, 'bob')
        self.assertIn('bob-cancel-agent', [i['name'] for i in self.api('GET', '/api/invites', author='bob')['invites']])
        self.assertEqual(self.api('POST', '/api/invites/cancel', {'name': 'bob-cancel-agent'}, 'bob')['cancelled'], 1)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.api('POST', '/api/join', {'code': invite['code']})
        self.assertEqual(error.exception.code, 410)
        slug = self.api('POST', '/api/projects', {'title': 'Temporary team'}, 'alice')['project']['slug']
        try:
            invite = self.api('POST', '/api/invites', {'name': 'bob'}, 'alice', slug)
            self.api('POST', '/api/join', {'code': invite['code']}, 'bob')
            invite = self.api('POST', '/api/invites', {'name': 'bob-left-agent', 'kind': 'agent'}, 'bob', slug)
            self.api('POST', '/api/members/remove', {'name': 'bob'}, 'bob', slug)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.api('POST', '/api/join', {'code': invite['code']})
            self.assertEqual(error.exception.code, 410)
        finally:
            self.api('POST', '/api/project/delete', {'confirm': DELETE_PHRASE}, 'alice', slug)


    def test_an_old_add_invite_keeps_the_current_role(self):
        slug = self.api('POST', '/api/projects', {'title': 'Role keep'}, 'alice')['project']['slug']
        try:
            invite = self.api('POST', '/api/invites', {'name': 'bob'}, 'alice', slug)
            self.api('POST', '/api/members/add', {'name': 'bob'}, 'alice', slug)
            self.api('POST', '/api/members/role', {'name': 'bob', 'role': 'owner'}, 'alice', slug)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.api('POST', '/api/join', {'code': invite['code']}, 'bob')
            self.assertEqual(error.exception.code, 409)
            self.assertEqual(self.api('GET', '/api/me', author='bob', project=slug)['project']['role'], 'owner')
        finally:
            self.api('POST', '/api/project/delete', {'confirm': DELETE_PHRASE}, 'alice', slug)

if __name__ == '__main__':
    unittest.main(verbosity=2)
