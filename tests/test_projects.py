"""Creator permissions and project identity through the real HTTP API and CLI."""
import base64
from contextlib import closing
import hashlib
import json
import subprocess
import sys
import time
import unittest
import urllib.error

import test_attachments as fixture


class ProjectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass()
        cls.fx = fixture.AttachmentTest()
        cls.s, cls.work, cls.url = cls.fx.s, cls.fx.work, cls.fx.url
        cls.fx.tokens['bob'] = 'tgt_' + cls.s.secrets.token_hex(24)
        with cls.s.DB_LOCK:
            con = cls.s.db()
            con.execute('INSERT INTO users(name,kind,owner,token_hash,created_ts,is_admin) VALUES (?,?,?,?,?,1)',
                        ('bob', 'human', 'bob', cls.s.token_hash(cls.fx.tokens['bob']), time.time()))
            cls.s.add_member(con, cls.fx.pid, 'bob', 'member', 'alice')

    @classmethod
    def tearDownClass(cls):
        fixture.AttachmentTest.tearDownClass()

    def api(self, method, path, data=None, who='alice', project=True):
        if project:
            path += ('&' if '?' in path else '?') + 'project=' + self.slug
        return self.fx.api(method, path, data, who=who)

    def error(self, code, method, path, data=None, **kwargs):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.api(method, path, data, **kwargs)
        self.assertEqual(raised.exception.code, code, raised.exception.read().decode())

    def setUp(self):
        self.p = self.api('POST', '/api/projects', {'title': 'Team project', 'slug': 'team-'+self.s.secrets.token_hex(4)}, project=False)['project']
        self.slug = self.p['slug']
        self.api('POST', '/api/members/add', {'name': 'bob'})
        name = 'agent-' + self.slug
        invite = self.api('POST', '/api/invites', {'name': name, 'kind': 'agent'})
        joined = self.fx.api('POST', '/api/join', {'code': invite['code']}, who=None)
        self.agent = name
        self.fx.tokens[name] = joined['token']

    def tearDown(self):
        # Tests may demote or remove the creator, so bob remains the cleanup owner.
        with self.s.DB_LOCK:
            self.s.db().execute("UPDATE members SET role='owner' WHERE project_id=? AND user='bob'", (self.p['id'],))
        self.api('POST', '/api/project/delete', {'confirm': self.slug}, who='bob')

    def rename(self, title, who='alice', **extra):
        return self.api('POST', '/api/project/rename', dict(title=title, **extra), who=who)

    def test_personal_project_order_persists_without_changing_other_accounts(self):
        before=self.api('GET','/api/projects',project=False)['projects']
        bob_before=self.api('GET','/api/projects',who='bob',project=False)['projects']
        ids=[p['id'] for p in reversed(before)]
        self.api('POST','/api/projects/order',{'project_ids':ids},project=False)
        self.assertEqual([p['id'] for p in self.api('GET','/api/projects',project=False)['projects']],ids)
        self.assertEqual([p['id'] for p in self.api('GET','/api/me')['projects']],ids)
        self.assertEqual(self.api('GET','/api/projects',who='bob',project=False)['projects'],bob_before)
        with self.s.DB_LOCK:
            self.s._db.close();self.s._db=None
        self.assertEqual([p['id'] for p in self.api('GET','/api/projects',project=False)['projects']],ids)
        self.api('POST','/api/projects/order',{'project_ids':[self.p['id']]},project=False)
        self.assertEqual(self.api('GET','/api/projects',project=False)['projects'][0]['id'],self.p['id'])

    def test_project_order_rejects_agents_foreign_projects_and_invalid_payloads(self):
        self.error(403,'POST','/api/projects/order',{'project_ids':[]},who=self.agent,project=False)
        self.error(404,'POST','/api/projects/order',{'project_ids':[self.p['id']+100000]},project=False)
        self.error(404,'POST','/api/projects/order',{'project_ids':[self.p['id']]},who='carol',project=False)
        for payload in ({},{'project_ids':'1'},{'project_ids':[True]},{'project_ids':['1']},
                        {'project_ids':[self.p['id']]*2},{'project_ids':[],'user':'bob'}):
            self.error(400,'POST','/api/projects/order',payload,project=False)

    def test_creator_can_rename_after_becoming_regular_member(self):
        self.api('POST', '/api/members/role', {'name': 'bob', 'role': 'owner'})
        self.api('POST', '/api/members/role', {'name': 'alice', 'role': 'member'}, who='bob')
        renamed = self.rename('  Новое название 🔥  ')['project']
        self.assertEqual(renamed['title'], 'Новое название 🔥')
        self.assertTrue(renamed['can_rename'])
        self.assertEqual(renamed['role'], 'member')
        self.error(403, 'POST', '/api/project/rename', {'title': 'Forged', 'created_by': 'bob'}, who='bob')
        self.assertFalse(self.api('GET', '/api/me', who='bob')['project']['can_rename'])

    def test_other_member_admin_owner_agent_and_outsider_cannot_rename(self):
        for who in ('bob', self.agent):
            self.error(403, 'POST', '/api/project/rename', {'title': 'Forged'}, who=who)
        self.api('POST', '/api/members/role', {'name': 'bob', 'role': 'owner'})
        self.error(403, 'POST', '/api/project/rename', {'title': 'Forged'}, who='bob')
        self.error(404, 'POST', '/api/project/rename', {'title': 'Forged'}, who='carol')
        self.error(401, 'POST', '/api/project/rename', {'title': 'Forged'}, who=None)
        self.assertEqual(self.api('GET', '/api/me')['project']['title'], 'Team project')

    def test_removed_creator_cannot_rename(self):
        self.api('POST', '/api/members/role', {'name': 'bob', 'role': 'owner'})
        self.api('POST', '/api/members/remove', {'name': 'alice'}, who='bob')
        self.error(404, 'POST', '/api/project/rename', {'title': 'Forged'})
        self.error(403, 'POST', '/api/project/rename', {'title': 'Forged'}, who='bob')

    def test_name_validation_is_shared_with_creation(self):
        invalid = [None, '', ' \t\n ', 42, [], {}, 'a'*81, 'two\nlines', 'tab\tname',
                   'null\x00name', 'escape\x1bname', 'line\u2028break', 'bad\ud800name', 'delete\x7fname']
        for title in invalid:
            with self.subTest(title=repr(title)):
                self.error(400, 'POST', '/api/project/rename', {'title': title})
                self.error(400, 'POST', '/api/projects', {'title': title}, project=False)
        self.assertEqual(self.rename('a'*80)['project']['title'], 'a'*80)
        self.assertEqual(self.rename('Команда 👩‍💻 & <тест>')['project']['title'], 'Команда 👩‍💻 & <тест>')
        title='ghp_'+'A'*36
        self.assertEqual(self.rename(title)['project']['title'],title)

    def test_rename_preserves_messages_members_files_invites_and_ids(self):
        raw = b'attachment fixture\n'
        md5 = hashlib.md5(raw).hexdigest()
        upload = self.api('POST', '/api/uploads', {'name': 'report.csv', 'size': len(raw), 'md5': md5})['id']
        self.api('POST', f'/api/uploads/{upload}/chunk?offset=0', raw)
        self.api('POST', f'/api/uploads/{upload}/complete', {})
        mid = self.api('POST', '/api/messages', {'body': 'History', 'attachments': [
            {'upload_id': upload}, {'name': 'old.txt', 'content_b64': base64.b64encode(raw).decode()}]})['id']
        invite = self.api('POST', '/api/invites', {'name': 'next-'+self.slug, 'kind': 'agent'})
        members = self.api('GET', '/api/members')['members']
        result = self.rename('Renamed', slug='forged', id=-1, created_by='bob')
        self.assertTrue(result['ok'])
        self.assertEqual(result['project']['id'], self.p['id'])
        self.assertEqual(result['project']['slug'], self.slug)
        self.assertEqual(result['project']['created_by'], 'alice')
        self.assertEqual(self.api('GET', '/api/members')['members'], members)
        message = self.api('GET', f'/api/messages/{mid}')['message']
        self.assertEqual(message['body'], 'History')
        for attachment in message['attachments']:
            with self.fx.request('GET', f"/api/attachments/{attachment['id']}", who='alice') as response:
                self.assertEqual(response.read(), raw)
        info = self.fx.api('GET', '/api/invite?code='+invite['code'], who=None)
        self.assertEqual(info['project_title'], 'Renamed')
        self.assertEqual(info['project'], self.slug)
        joined = self.fx.api('POST', '/api/join', {'code': invite['code']}, who=None)
        self.assertEqual(joined['project'], self.slug)
        agent_me = self.api('GET', '/api/me', who=self.agent)['project']
        self.assertEqual(agent_me['title'], 'Renamed')
        self.assertFalse(agent_me['can_rename'])

    def test_creator_account_rename_keeps_rights_and_cli_uses_same_api(self):
        config = self.work/'human-client.json'
        config.write_text(json.dumps({'server': self.url, 'token': self.fx.tokens['alice'],
                                      'project': self.slug, 'url_source': ''}), encoding='utf-8')
        self.api('POST', '/api/rename', {'new': 'alice-renamed'})
        try:
            result = subprocess.run([sys.executable, '-X', 'utf8', str(fixture.ROOT/'tg.py'), '--config', str(config),
                                     'projects', 'rename', '  CLI title  '], env=self.fx.env, cwd=self.work,
                                    capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('CLI title', result.stdout)
            self.assertEqual(self.api('GET', '/api/me')['project']['created_by'], 'alice-renamed')
            config.write_text(json.dumps({'server': self.url, 'token': self.fx.tokens[self.agent],
                                          'project': self.slug, 'url_source': ''}), encoding='utf-8')
            denied = subprocess.run([sys.executable, '-X', 'utf8', str(fixture.ROOT/'tg.py'), '--config', str(config),
                                     'projects', 'rename', 'Forged'], env=self.fx.env, cwd=self.work,
                                    capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(denied.returncode, 2, denied.stderr)
            self.assertIn('создатель', denied.stderr)
        finally:
            self.api('POST', '/api/rename', {'new': 'alice'})

    def test_migration_marker_does_not_grant_creator_rights(self):
        with self.s.DB_LOCK:
            self.s.db().execute("UPDATE projects SET created_by='bob',creator_unknown=1 WHERE id=?", (self.p['id'],))
        self.error(403, 'POST', '/api/project/rename', {'title': 'Forged'}, who='bob')
        info = self.api('GET', '/api/me', who='bob')['project']
        self.assertIsNone(info['created_by'])
        self.assertFalse(info['can_rename'])

    def test_old_database_upgrade_keeps_unknown_creator_unknown(self):
        path = self.work/'legacy.db'
        with closing(self.s.sqlite3.connect(path)) as con, con:
            con.executescript(self.s.SCHEMA)
            con.execute("INSERT INTO projects(slug,title,created_by,created_ts) VALUES ('legacy','Legacy','migration',1)")
            con.execute("INSERT INTO projects(slug,title,created_by,created_ts) VALUES ('console','Console','cli',1)")
        spec = fixture.importlib.util.spec_from_file_location('legacy_project_server', fixture.ROOT/'server.py')
        legacy = fixture.importlib.util.module_from_spec(spec); spec.loader.exec_module(legacy)
        legacy.Cfg.db = str(path)
        try:
            con = legacy.db()
            project = con.execute("SELECT p.*, 'owner' AS role FROM projects p WHERE slug='legacy'").fetchone()
            self.assertEqual(project['title'], 'Legacy')
            self.assertEqual(project['created_by'], 'migration')
            self.assertTrue(project['creator_unknown'])
            self.assertFalse(legacy.can_rename_project(project, {'kind':'human', 'name':'migration'}))
            con.close(); legacy._db = None
            self.assertTrue(legacy.db().execute('SELECT creator_unknown FROM projects').fetchone()[0])
            self.assertTrue(legacy.db().execute("SELECT creator_unknown FROM projects WHERE slug='console'").fetchone()[0])
            # A real account with this name may still create its own new project.
            pid = legacy.create_project_row(legacy.db(), 'new-team', 'New team', 'migration')
            project = legacy.db().execute("SELECT p.*, 'owner' AS role FROM projects p WHERE id=?", (pid,)).fetchone()
            self.assertTrue(legacy.can_rename_project(project, {'kind':'human', 'name':'migration'}))
        finally:
            if legacy._db is not None: legacy._db.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
