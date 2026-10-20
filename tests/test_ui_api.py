"""UI metadata respects visibility; locale changes error copy, never authority or data."""
import json
import re
import secrets
import unittest
import urllib.error
import test_attachments as fixture


class UIAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest()
        cls.fx.tokens['bob']='tgt_'+secrets.token_hex(24)
        cls.fx.s.db().execute('INSERT INTO users(name,kind,owner,token_hash,created_ts) VALUES (?,?,?,?,?)',
                             ('bob','human','bob',cls.fx.s.token_hash(cls.fx.tokens['bob']),cls.fx.s.time.time()))
        cls.fx.s.add_member(cls.fx.s.db(),cls.fx.pid,'bob','member','alice')

    @classmethod
    def tearDownClass(cls):
        fixture.AttachmentTest.tearDownClass()

    def test_activity_is_self_reported_scoped_and_expires(self):
        payload={'state':'working','client':'a'*32}
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.fx.api('POST','/api/activity?project=main',payload,who='alice')
        self.assertEqual(denied.exception.code,403)
        self.fx.api('POST','/api/activity?project=main',payload,who='alice-agent')
        def working(name):
            return next(u for u in self.fx.api('GET','/api/me?project=main',who='alice')['users'] if u['name']==name)['working']
        self.assertTrue(working('alice-agent'))
        self.assertIsNone(working('bob-agent'))
        with self.assertRaises(urllib.error.HTTPError) as forged:
            self.fx.api('POST','/api/activity?project=main',dict(payload,account_id='bob-agent'),who='alice-agent')
        self.assertEqual(forged.exception.code,400)
        self.fx.api('POST','/api/activity?project=main',dict(payload,state='idle',client='b'*32),who='alice-agent')
        self.assertTrue(working('alice-agent'),'idle session must not clear another active session')
        self.fx.api('POST','/api/activity?project=main',dict(payload,state='idle'),who='alice-agent')
        self.assertFalse(working('alice-agent'))
        self.fx.api('POST','/api/activity?project=main',payload,who='alice-agent')
        row=self.fx.s.db().execute("SELECT * FROM users WHERE name='alice-agent'").fetchone()
        self.assertIsNone(self.fx.s.presence(row,self.fx.s.time.time()+self.fx.s.ACTIVITY_WINDOW+1)['working'])

    def test_addressed_receipts_distinguish_chat_observers_from_recipients(self):
        mid=self.fx.api('POST','/api/messages',{'body':'addressed receipt fixture','to_ids':['alice-agent']},who='alice')['id']
        self.fx.api('POST','/api/receipts',{'kind':'ack','ids':[mid]},who='alice-agent')
        # A human's open page fetches the message, but that person is not its addressee.
        self.fx.api('GET',f'/api/messages?after={mid-1}',who='bob')
        message=self.fx.api('GET',f'/api/messages/{mid}',who='alice')['message']
        receipts={r['user_id']:r for r in message['receipts']}
        self.assertTrue(receipts['alice-agent']['is_recipient'])
        self.assertTrue(receipts['alice-agent']['read']);self.assertTrue(receipts['alice-agent']['acked'])
        self.assertFalse(receipts['bob']['is_recipient']);self.assertFalse(receipts['bob']['read'])
        self.assertTrue(all(r['read'] for r in message['receipts'] if r['is_recipient']))
        self.assertFalse(all(r['read'] for r in message['receipts']))

    def test_broadcast_receipts_keep_each_addressed_agent_state(self):
        mid=self.fx.api('POST','/api/messages',{'body':'broadcast receipt fixture'},who='alice')['id']
        self.fx.api('GET',f'/api/messages?after={mid-1}',who='bob')
        self.fx.api('POST','/api/receipts',{'kind':'ack','ids':[mid]},who='alice-agent')
        message=self.fx.api('GET',f'/api/messages/{mid}',who='alice')['message']
        addressed=[r for r in message['receipts'] if r['is_recipient']]
        self.assertEqual({r['user_id'] for r in addressed},{'alice-agent','bob-agent'})
        self.assertFalse(all(r['read'] for r in addressed))
        self.fx.api('POST','/api/receipts',{'kind':'read','ids':[mid]},who='bob-agent')
        message=self.fx.api('GET',f'/api/messages/{mid}',who='alice')['message']
        self.assertTrue(all(r['read'] for r in message['receipts'] if r['is_recipient']))

    def test_language_is_request_local_and_does_not_change_access(self):
        for language,english in [('en',True),('ru',False),('invalid',False),('en',True),('ru',False)]:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.fx.api('POST','/api/project/rename?project=main',{'title':''},who='alice',headers={'X-Telegent-Language':language})
            self.assertEqual(raised.exception.code,400)
            error=json.load(raised.exception)['error']
            self.assertEqual(not bool(re.search('[А-Яа-яЁё]',error)),english,error)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.fx.api('GET','/api/messages?project=other',headers={'X-Telegent-Language':'en'})
        self.assertEqual(denied.exception.code,404)

    def test_waiting_dismissal_is_personal_persistent_and_preserves_requests(self):
        def send(body, recipients):
            return self.fx.api('POST','/api/messages?project=main',
                               {'body':body,'type':'request','to_ids':recipients})['id']
        def waiting(who='alice'):
            return {m['id'] for m in self.fx.api('GET','/api/messages?project=main&waiting_me=1&conversation=all',who=who)['messages']}
        shared=send('shared reminder fixture',['alice','bob'])
        second=send('second reminder fixture',['alice'])
        self.assertIn(shared,waiting())
        self.assertIn(shared,waiting('bob'))
        self.fx.api('POST','/api/waiting/dismiss?project=main',{'ids':[shared]},who='alice')
        self.assertNotIn(shared,waiting())
        self.assertIn(shared,waiting('bob'))
        row=self.fx.api('GET',f'/api/messages/{shared}',who='alice')['message']
        self.assertEqual(row['status']['state'],'open')
        self.assertFalse(any(r['acked'] for r in row['receipts']))
        self.assertEqual(self.fx.api('POST','/api/waiting/dismiss?project=main',{'ids':[shared]},who='alice')['dismissed'],0)
        self.fx.api('POST','/api/waiting/dismiss?project=main',{'all':True},who='alice')
        self.assertNotIn(second,waiting())
        new=send('new reminder fixture',['alice'])
        self.assertIn(new,waiting())
        self.assertNotIn(shared,waiting())
        # Dismissals live in the persistent store, independent of a browser session.
        self.assertIsNotNone(self.fx.s.db().execute('SELECT 1 FROM waiting_dismissals WHERE user=? AND message_id=?',('alice',shared)).fetchone())
        edited=self.fx.api('POST','/api/messages?project=main',
                           {'body':'editable reminder fixture','type':'question','to_ids':['alice']},who='bob')['id']
        self.fx.api('POST','/api/waiting/dismiss?project=main',{'ids':[edited]},who='alice')
        self.fx.api('POST',f'/api/messages/{edited}/edit',{'revision':0,'body':'updated reminder fixture'},who='bob')
        self.assertIn(edited,waiting(),'an edited question must not stay silently dismissed')

    def test_waiting_dismissal_rejects_foreign_ids_and_cannot_forge_authority(self):
        own=self.fx.api('POST','/api/messages?project=main',{'body':'own reminder fixture','type':'question','to_ids':['alice']})['id']
        foreign=self.fx.api('POST','/api/messages?project=other',{'body':'foreign reminder fixture'},who='carol')['id']
        cid=self.fx.api('POST','/api/conversations?project=main',{'peer_id':'alice-agent'},who='bob')['conversation']['id']
        private=self.fx.api('POST','/api/messages?project=main',{'body':'private reminder fixture','conversation_id':cid},who='bob')['id']
        for mid in (foreign,private):
            with self.assertRaises(urllib.error.HTTPError) as denied:
                self.fx.api('POST','/api/waiting/dismiss?project=main',{'ids':[own,mid]},who='alice')
            self.assertEqual(denied.exception.code,404)
        self.assertIsNone(self.fx.s.db().execute('SELECT 1 FROM waiting_dismissals WHERE user=? AND message_id=?',('alice',own)).fetchone())
        for payload in ({'ids':[own],'user':'bob'},{'all':False},{'ids':[]},{'ids':[True]},{'ids':[own],'all':True}):
            with self.assertRaises(urllib.error.HTTPError) as denied:
                self.fx.api('POST','/api/waiting/dismiss?project=main',payload,who='alice')
            self.assertEqual(denied.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.fx.api('POST','/api/waiting/dismiss?project=main',{'all':True})
        self.assertEqual(denied.exception.code,403)

    def test_thread_count_is_scoped_and_excludes_service_messages(self):
        root=self.fx.api('POST','/api/messages?project=main',{'body':'fixture root'},who='alice')['id']
        self.fx.api('POST','/api/messages?project=main',{'body':'reply fixture','reply_to':root})
        foreign=self.fx.api('POST','/api/messages?project=other',{'body':'unrelated fixture'},who='carol')['id']
        with self.fx.s.DB_LOCK:
            self.fx.s.db().execute('UPDATE messages SET thread_id=?,reply_to=? WHERE id=?',(root,root,foreign))
        row=self.fx.api('GET',f'/api/messages/{root}',who='alice')['message']
        self.assertEqual(row['reply_count'],1)
        cid=self.fx.api('POST','/api/conversations?project=main',{'peer_id':'alice-agent'},who='alice')['conversation']['id']
        private=self.fx.api('POST','/api/messages?project=main',{'body':'private root','conversation_id':cid},who='alice')['id']
        self.fx.api('POST','/api/messages?project=main',{'body':'private answer','reply_to':private,'conversation_id':cid},who='alice-agent')
        private_row=self.fx.api('GET',f'/api/messages/{private}',who='alice')['message']
        self.assertEqual(private_row['reply_count'],1)
        public=self.fx.api('GET','/api/messages?project=main')['messages']
        self.assertNotIn(private,{m['id'] for m in public})
        self.assertEqual(next(m for m in public if m['id']==root)['reply_count'],1)

    def test_reply_preview_uses_direct_parent_body_and_survives_pagination(self):
        root=self.fx.api('POST','/api/messages?project=main',{'body':'root fixture'},who='alice')['id']
        parent=self.fx.api('POST','/api/messages?project=main',{'body':'answer first line\nanswer second line','reply_to':root})['id']
        child=self.fx.api('POST','/api/messages?project=main',{'body':'reply to answer','reply_to':parent},who='alice')['id']
        result=self.fx.api('GET',f'/api/messages?project=main&after={parent}',who='alice')['messages']
        preview=next(m for m in result if m['id']==child)['reply_preview']
        self.assertEqual(preview['id'],parent)
        self.assertEqual(preview['body'],'answer first line\nanswer second line')
        self.assertNotEqual(preview['id'],root)
        self.fx.api('POST',f'/api/messages/{root}/delete',{'revision':0},who='alice')
        self.assertTrue(self.fx.api('GET',f'/api/messages/{parent}',who='alice')['message']['reply_preview']['deleted'])
        self.assertEqual(self.fx.api('GET',f'/api/messages/{parent}',who='alice')['message']['reply_preview']['body'],'')

    def test_reply_preview_does_not_cross_project_or_conversation(self):
        public=self.fx.api('POST','/api/messages?project=main',{'body':'public preview fixture'},who='alice')['id']
        foreign=self.fx.api('POST','/api/messages?project=other',{'body':'foreign preview fixture'},who='carol')['id']
        cid=self.fx.api('POST','/api/conversations?project=main',{'peer_id':'alice-agent'},who='alice')['conversation']['id']
        private=self.fx.api('POST','/api/messages?project=main',{'body':'private preview fixture','conversation_id':cid},who='alice')['id']
        for parent in (foreign,private):
            with self.fx.s.DB_LOCK:
                self.fx.s.db().execute('UPDATE messages SET reply_to=? WHERE id=?',(parent,public))
            for who in ('alice','alice-agent'):
                self.assertIsNone(self.fx.api('GET',f'/api/messages/{public}',who=who)['message']['reply_preview'])


if __name__=='__main__':unittest.main(verbosity=2)
