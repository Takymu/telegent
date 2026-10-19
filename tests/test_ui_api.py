"""UI metadata respects visibility; locale changes error copy, never authority or data."""
import json
import re
import unittest
import urllib.error
import test_attachments as fixture


class UIAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest()

    @classmethod
    def tearDownClass(cls):
        fixture.AttachmentTest.tearDownClass()

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


if __name__=='__main__':unittest.main(verbosity=2)
