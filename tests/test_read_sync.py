"""Two independent sessions share read state, without sharing access or acknowledgements."""
import concurrent.futures
import time
import unittest
import urllib.error

import test_attachments as fixture


class ReadSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass(); cls.fx=fixture.AttachmentTest(); cls.s=cls.fx.s
        cls.fx.api('POST','/api/password',{'new':'alice fixture password'},who='alice')
        for device in ('desktop','mobile'):
            cls.fx.tokens[device]=cls.fx.api('POST','/api/login',{'name':'alice','password':'alice fixture password'},who=None)['token']

    @classmethod
    def tearDownClass(cls):fixture.AttachmentTest.tearDownClass()

    def setUp(self):
        self.slug='team-'+self.s.secrets.token_hex(4)
        self.p=self.fx.api('POST','/api/projects',{'title':'Read fixture','slug':self.slug},who='alice')['project']
        self.agent='helper-'+self.s.secrets.token_hex(4)
        invite=self.api('POST','/api/invites',{'name':self.agent,'kind':'agent'})
        self.fx.tokens[self.agent]=self.fx.api('POST','/api/join',{'code':invite['code']},who=None)['token']

    def tearDown(self):self.api('POST','/api/project/delete',{'confirm':self.slug})

    def api(self,method,path,data=None,who='desktop'):
        return self.fx.api(method,path+('&' if '?' in path else '?')+'project='+self.slug,data,who=who)

    def project(self,who='desktop'):
        return next(p for p in self.api('GET','/api/projects',who=who)['projects'] if p['slug']==self.slug)

    def incoming(self):return self.api('POST','/api/messages',{'body':'fixture incoming','to':['alice']},who=self.agent)['id']

    def test_own_message_from_another_session_is_never_unread(self):
        mid=self.api('POST','/api/messages',{'body':'sent on desktop'})['id']
        self.assertEqual(self.project('mobile')['unread'],0)
        message=self.api('GET',f'/api/messages/{mid}',who='mobile')['message']
        self.assertEqual(message['author_id'],self.api('GET','/api/me',who='mobile')['id'])

    def test_read_on_mobile_updates_desktop_without_ack_or_status_change(self):
        mid=self.incoming()
        self.assertEqual(self.project()['unread'],1)
        self.api('POST','/api/receipts',{'kind':'read','ids':[mid]},who='mobile')
        self.assertEqual(self.project()['unread'],0)
        message=self.api('GET',f'/api/messages/{mid}')['message']
        self.assertTrue(message['read']);self.assertFalse(message['acked'])
        listed=self.api('GET','/api/projects?seen='+self.slug+':0')['projects']
        self.assertEqual(next(p for p in listed if p['slug']==self.slug)['unread'],0)

    def test_cursor_is_monotonic_and_cannot_read_future_messages(self):
        first=self.incoming();last=self.incoming()
        result=self.api('POST','/api/receipts',{'kind':'read','ids':[last],'seen_upto':last+100000},who='mobile')
        self.assertEqual(result['read_upto'],last)
        self.assertEqual(self.project()['unread'],0)
        new=self.incoming()
        self.assertEqual(self.project()['unread'],1)
        result=self.api('POST','/api/receipts',{'kind':'read','seen_upto':first})
        self.assertEqual(result['read_upto'],last)
        self.assertEqual(self.project()['first_unread_id'],new)

    def test_foreign_project_and_account_are_not_mutable(self):
        mid=self.incoming()
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.api('POST','/api/receipts',{'kind':'read','ids':[mid],'seen_upto':mid},who='carol')
        self.assertEqual(raised.exception.code,404)
        self.assertEqual(self.project()['unread'],1)
        self.api('POST','/api/receipts',{'kind':'read','ids':[mid],'seen_upto':mid,'user':self.agent})
        agent_message=self.api('GET',f'/api/messages/{mid}',who=self.agent)['message']
        self.assertFalse(any(r.get('user_id')==self.agent and r['read'] for r in agent_message['receipts']))
        self.s.db().execute('DELETE FROM members WHERE project_id=? AND user=?',(self.p['id'],self.agent))
        with self.assertRaises(urllib.error.HTTPError):self.api('POST','/api/receipts',{'seen_upto':mid},who=self.agent)

    def test_receipt_changes_wake_browser_without_new_messages(self):
        mid=self.incoming()
        before=self.api('GET','/api/messages')
        path=f"/api/wait?after={before['last_id']}&for_me=0&timeout=2&receipt_revision={before['receipt_revision']}"
        with concurrent.futures.ThreadPoolExecutor() as pool:
            start=time.monotonic();pending=pool.submit(self.api,'GET',path)
            time.sleep(.1);self.api('POST','/api/receipts',{'kind':'read','ids':[mid],'seen_upto':mid},who='mobile')
            result=pending.result(timeout=3)
        self.assertLess(time.monotonic()-start,1.5)
        self.assertEqual(result['messages'],[]);self.assertEqual(result['last_id'],before['last_id'])
        self.assertNotEqual(result['receipt_revision'],before['receipt_revision'])
        revision=result['receipt_revision']
        self.api('POST','/api/receipts',{'kind':'read','ids':[mid],'seen_upto':mid},who='mobile')
        self.assertEqual(self.api('GET','/api/messages')['receipt_revision'],revision)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            start=time.monotonic();pending=pool.submit(self.api,'GET',f"/api/wait?after={before['last_id']}&for_me=1&timeout=0.5",None,self.agent)
            time.sleep(.1);self.api('POST','/api/receipts',{'kind':'ack','ids':[mid]})
            self.assertEqual(pending.result(timeout=2)['messages'],[])
        self.assertGreater(time.monotonic()-start,.35)

    def test_validation_and_persistence_after_database_reopen(self):
        mid=self.incoming()
        for value in (-1, True, '12', 2**63):
            with self.assertRaises(urllib.error.HTTPError):self.api('POST','/api/receipts',{'seen_upto':value})
        self.api('POST','/api/receipts',{'kind':'read','ids':[mid],'seen_upto':mid})
        with self.s.DB_LOCK:
            self.s._db.close();self.s._db=None;self.s.db()
        self.assertEqual(self.project('mobile')['read_upto'],mid)
        self.assertEqual(self.project('mobile')['unread'],0)


if __name__=='__main__':unittest.main(verbosity=2)
