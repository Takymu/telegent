"""Shared materials preserve source visibility, pagination and read state without reading blobs."""
import base64
import unittest
import urllib.error
import urllib.parse
import test_attachments as fixture


class SharedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest();cls.s=cls.fx.s

    @classmethod
    def tearDownClass(cls):fixture.AttachmentTest.tearDownClass()

    def send(self,name,body='Shared fixture',**kw):
        return self.fx.api('POST','/api/messages',{'body':body,'attachments':[{'name':name,'content_b64':base64.b64encode(b'fixture').decode()}],**kw},who='alice')['id']
    def shared(self,kind='files',extra='',who='bob-agent'):
        return self.fx.api('GET','/api/shared?project=main&kind='+kind+extra,who=who)
    def error(self,code,path,who='bob-agent'):
        with self.assertRaises(urllib.error.HTTPError) as raised:self.fx.api('GET',path,who=who)
        self.assertEqual(raised.exception.code,code)

    def test_categories_literal_search_and_metadata_only(self):
        photo=self.send('PHOTO.PNG');file=self.send('Report_100%.MD')
        self.assertIn(photo,[i['message_id'] for i in self.shared('photos')['items']])
        files=self.shared('files','&q=100%25')['items']
        self.assertEqual([i['message_id'] for i in files],[file])
        self.assertTrue(files[0]['attachment']['inline'])
        self.assertFalse(any('content' in i['attachment'] or 'content_b64' in i['attachment'] for i in files))
        legacy=self.fx.api('POST','/api/messages',{'body':'Legacy fixture','attachments':[{'name':'remote.png','link':'https://example.invalid/image.png'}]},who='alice')['id']
        self.assertNotIn(legacy,[i['message_id'] for i in self.shared('photos')['items']])
        self.assertIn(legacy,[i['message_id'] for i in self.shared('files')['items']])
        self.assertIn(legacy,[i['message_id'] for i in self.shared('links')['items']])

    def test_link_parsing_and_search_are_http_only_and_do_not_fetch_sites(self):
        mid=self.fx.api('POST','/api/messages',{'body':'[docs](https://example.invalid/a_(b)?x=1&y=2), https://other.invalid/one. javascript:alert(1) file:///local'},who='alice')['id']
        item=next(i for i in self.shared('links','&q=example.invalid')['items'] if i['message_id']==mid)
        self.assertEqual(item['links'],['https://example.invalid/a_(b)?x=1&y=2'])
        self.assertEqual(self.s.shared_urls('https://[invalid http:// https://example.invalid/end!'),['https://example.invalid/end'])

    def test_cursor_covers_all_attachments_including_multiple_per_message(self):
        mid=self.send('page-fixture.txt')
        with self.s.DB_LOCK:
            for i in range(46):self.s.db().execute('INSERT INTO attachments(message_id,name,size,content) VALUES(?,?,?,?)',(mid,f'page-{i}.txt',1,b'x'))
        page=self.shared('files','&q=page-');ids=[i['attachment']['id'] for i in page['items']]
        self.assertEqual(len(ids),40);self.assertIsNotNone(page['next_before'])
        page2=self.shared('files','&q=page-&before='+str(page['next_before']))
        rest=[i['attachment']['id'] for i in page2['items']]
        self.assertEqual(len(ids+rest),47);self.assertEqual(len(set(ids+rest)),47)

    def test_links_paginate_and_secret_cards_are_excluded(self):
        mids=[]
        for i in range(23):
            mids.append(self.fx.api('POST','/api/messages',{'body':f'https://page-link.invalid/{i}'},who='alice')['id'])
        first=self.shared('links','&q=page-link.invalid')
        second=self.shared('links','&q=page-link.invalid&before='+str(first['next_before']))
        self.assertEqual(set(mids),{i['message_id'] for i in first['items']+second['items']})
        hidden=self.send('hidden-secret.txt','https://hidden-secret.invalid/fixture')
        with self.s.DB_LOCK:self.s.db().execute("UPDATE messages SET type='secret' WHERE id=?",(hidden,))
        for kind in ('files','links'):
            self.assertNotIn(hidden,[i['message_id'] for i in self.shared(kind)['items']])

    def test_dm_foreign_projects_and_deleted_sources_stay_separate(self):
        dm=self.fx.api('POST','/api/conversations',{'peer_id':'bob-agent'},who='alice')['conversation']['id']
        mid=self.send('private-shared.txt','https://private.invalid/fixture',conversation_id=dm)
        self.assertNotIn(mid,[i['message_id'] for i in self.shared()['items']])
        self.assertIn(mid,[i['message_id'] for i in self.shared(extra='&conversation='+dm)['items']])
        self.error(404,'/api/shared?project=main&kind=files&conversation='+dm,who='alice-agent')
        self.error(404,'/api/shared?project=main&kind=files',who='carol')
        self.error(404,'/api/shared?project=other&kind=files')
        before=self.shared(extra='&conversation='+dm)['revision']
        self.fx.api('POST',f'/api/messages/{mid}/delete',{'revision':0},who='alice')
        after=self.shared(extra='&conversation='+dm)
        self.assertNotIn(mid,[i['message_id'] for i in after['items']]);self.assertNotEqual(before,after['revision'])

    def test_browsing_does_not_mark_delivered_read_or_ack(self):
        mid=self.send('unread-library.txt')
        before=[tuple(r) for r in self.s.db().execute('SELECT * FROM receipts WHERE message_id=?',(mid,))]
        self.shared(who='alice');self.shared();self.shared('photos');self.shared('links')
        self.assertEqual(before,[tuple(r) for r in self.s.db().execute('SELECT * FROM receipts WHERE message_id=?',(mid,))])

    def test_chat_search_finds_filenames_once_and_respects_visibility(self):
        name='Итоговый_100%-fixture.PDF'
        public=self.send(name)
        with self.s.DB_LOCK:
            self.s.db().execute('INSERT INTO attachments(message_id,name,size,content) VALUES(?,?,?,?)',(public,name,1,b'x'))
        cid=self.fx.api('POST','/api/conversations',{'peer_id':'bob-agent'},who='alice')['conversation']['id']
        private=self.send(name,conversation_id=cid)
        foreign=self.fx.api('POST','/api/messages?project=other',{'body':'Fixture','attachments':[
            {'name':name,'content_b64':base64.b64encode(b'x').decode()}]},who='carol')['id']
        q=urllib.parse.quote(name.lower())
        def search(extra='',who='bob-agent'):
            return [m['id'] for m in self.fx.api('GET','/api/messages?project=main&q='+q+extra,who=who)['messages']]
        self.assertEqual(search(),[public])
        self.assertEqual(search('&conversation='+cid),[private])
        self.assertEqual(search('&conversation=all'),[public,private])
        self.assertEqual(search('&conversation=all',who='alice-agent'),[public])
        self.assertNotIn(foreign,search('&conversation=all'))
        self.fx.api('POST',f'/api/messages/{public}/delete',{'revision':0},who='alice')
        self.assertEqual(search(),[])

    def test_extension_filter_facets_cover_history_and_remain_private(self):
        pdf=self.send('ext-fixture.PDF');md=self.send('ext-fixture.md');plain=self.send('ext-fixture')
        self.send('ext-fixture.png')
        cid=self.fx.api('POST','/api/conversations',{'peer_id':'bob-agent'},who='alice')['conversation']['id']
        private=self.send('ext-fixture.private',conversation_id=cid)
        def page(extra=''):return self.shared('files','&q=ext-fixture'+extra)
        selected=page('&ext=.PDF')
        self.assertEqual([r['message_id'] for r in selected['items']],[pdf])
        self.assertEqual({t['extension'] for t in selected['file_types']},{'.pdf','.md',''})
        self.assertEqual([r['message_id'] for r in page('&ext=none')['items']],[plain])
        self.assertEqual([r['message_id'] for r in page('&ext=.md')['items']],[md])
        self.assertEqual(page('&ext=.zip')['items'],[])
        self.assertEqual(page('&ext=.pdf&before=1')['file_types'],selected['file_types'])
        self.assertIn(private,[r['message_id'] for r in page('&conversation='+cid+'&ext=.private')['items']])
        self.assertNotIn('.private',[t['extension'] for t in page()['file_types']])
        self.fx.api('POST',f'/api/messages/{md}/delete',{'revision':0},who='alice')
        self.assertNotIn('.md',[t['extension'] for t in page()['file_types']])

    def test_validation(self):
        for extra in ('&before=-1','&before=0','&before=bad','&before='+str(2**63),'&before='+'9'*5000,'&q='+'a'*201):
            self.error(400,'/api/shared?project=main&kind=files'+extra)
        self.error(400,'/api/shared?project=main&kind=secret')
        self.error(404,'/api/shared?project=main&conversation=all')
        self.error(401,'/api/shared?project=main',who=None)
        for suffix in ('&ext=pdf','&ext=../pdf','&ext=.tar.gz','&ext=.%00','&ext=.%20'):
            self.error(400,'/api/shared?project=main&kind=files'+suffix)
        self.error(400,'/api/shared?project=main&kind=photos&ext=.png')


if __name__=='__main__':unittest.main(verbosity=2)
