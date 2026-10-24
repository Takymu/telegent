"""Durable administrative update notices without cross-project read permissions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import types
import unittest
import uuid

import test_attachments as fixture


class AgentNoticeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass()
        cls.fx = fixture.AttachmentTest()
        cls.s = cls.fx.s
        cls.s.db().execute("UPDATE users SET is_admin=1 WHERE name='alice'")
        token = cls.fx.work / 'private-agent.token'
        with redirect_stdout(io.StringIO()):
            cls.s.cmd_adduser(types.SimpleNamespace(name='private-agent', kind='agent', owner='carol',
                             project='other', rotate=False, token_out=str(token)))
        cls.fx.tokens['private-agent'] = token.read_text().strip()

    @classmethod
    def tearDownClass(cls):
        fixture.AttachmentTest.tearDownClass()

    def notice(self, **extra):
        body = {'body':'Client maintenance fixture. Update your client and read the protocol.',
                'key':'release-'+uuid.uuid4().hex}
        body.update(extra)
        return body

    def send(self, body):
        return self.fx.api('POST', '/api/admin/agent-notice', body, who='alice')

    def test_only_authorized_human_admin_may_send(self):
        data = self.notice()
        before = self.s.db().execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        for who in ('carol', 'bob-agent', None):
            self.fx.error(401 if who is None else 403, 'POST', '/api/admin/agent-notice', data, who=who)
        self.s.db().execute("UPDATE users SET is_admin=1 WHERE name='bob-agent'")
        try:
            self.fx.error(403, 'POST', '/api/admin/agent-notice', data, who='bob-agent')
        finally:
            self.s.db().execute("UPDATE users SET is_admin=0 WHERE name='bob-agent'")
        self.fx.error(400, 'POST', '/api/admin/agent-notice', dict(data, author='carol'), who='alice')
        self.assertEqual(before, self.s.db().execute('SELECT COUNT(*) FROM messages').fetchone()[0])

    def test_offline_agents_receive_persistent_notices_without_opening_their_projects(self):
        data = self.notice()
        memberships = list(map(tuple, self.s.db().execute('SELECT project_id,user,role FROM members ORDER BY project_id,user')))
        self.assertFalse(self.fx.api('GET', '/api/me', who='private-agent')['listening'])
        self.assertEqual(self.send(data), {'ok':True, 'projects':2, 'agents':3, 'created':2})
        messages = {}
        for agent, project in (('alice-agent','main'), ('bob-agent','main'), ('private-agent','other')):
            # No watch was running at send time. An old client's ordinary inbox request still finds it.
            rows = self.fx.api('GET', '/api/messages?for_me=1&unacked=1&project='+project, who=agent)['messages']
            message = next(m for m in rows if m['body'].endswith(data['body']))
            self.assertEqual(message['type'],'info'); self.assertEqual(message['author_kind'],'human')
            self.assertEqual(message['author_id'],'alice')
            self.assertFalse(message['read']); self.assertFalse(message['acked'])
            self.assertNotIn('carol',message['to_ids'])
            messages[agent] = message
        self.assertEqual(messages['alice-agent']['id'],messages['bob-agent']['id'])
        private = messages['private-agent']['id']
        self.fx.error(404, 'GET', '/api/messages/'+str(private), who='alice')
        self.fx.error(404, 'GET', '/api/messages?project=other', who='alice')
        self.assertEqual(memberships, list(map(tuple,self.s.db().execute('SELECT project_id,user,role FROM members ORDER BY project_id,user'))))
        self.assertIsNone(self.s.db().execute('SELECT ack_ts FROM receipts WHERE message_id=? AND user=?', (private,'private-agent')).fetchone()[0])

    def test_concurrent_retries_keep_one_notice_per_project(self):
        data = self.notice()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _:self.send(data),range(3)))
        self.assertEqual(sum(r['created'] for r in results),2)
        rows = self.s.db().execute('SELECT id FROM messages WHERE idem_key LIKE ?',('agent-notice:'+data['key']+':%',)).fetchall()
        self.assertEqual(len(rows),2)
        self.fx.error(409,'POST','/api/admin/agent-notice',dict(data,body='different fixture'),who='alice')
        self.assertEqual(self.send(data)['created'],0)

    def test_invalid_text_is_rejected_before_broadcast(self):
        before = self.s.db().execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        for data,code in ((self.notice(body=' '),400), (self.notice(key=''),400),
                          (self.notice(body='x'*self.s.MAX_BODY_CHARS),413)):
            self.fx.error(code,'POST','/api/admin/agent-notice',data,who='alice')
        self.assertEqual(before,self.s.db().execute('SELECT COUNT(*) FROM messages').fetchone()[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
