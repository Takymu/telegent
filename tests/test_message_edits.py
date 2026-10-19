"""Human authorship, stable threads, private edits, deletion and version-bound approval."""
import base64
import concurrent.futures
import hashlib
import secrets
import time
import unittest
import urllib.error
from unittest import mock

import test_attachments as fixture


class MessageEditsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest();cls.s=cls.fx.s
        cls.fx.tokens['bob']='tgt_'+secrets.token_hex(24)
        with cls.s.DB_LOCK:
            con=cls.s.db()
            con.execute('INSERT INTO users(name,kind,owner,token_hash,created_ts) VALUES (?,?,?,?,?)',
                        ('bob','human','bob',cls.s.token_hash(cls.fx.tokens['bob']),time.time()))
            cls.s.add_member(con,cls.fx.pid,'bob','member','alice')
            con.execute("UPDATE users SET is_admin=1 WHERE name='alice'")

    @classmethod
    def tearDownClass(cls):fixture.AttachmentTest.tearDownClass()

    def setUp(self):
        with self.s.DB_LOCK:
            for table in ('approval_history','secret_envelopes','approvals','attachments','receipts','reactions','messages','conversations'):
                self.s.db().execute('DELETE FROM '+table)

    def api(self,method,path,data=None,who='alice',project='main'):
        return self.fx.api(method,path+('&' if '?' in path else '?')+'project='+project,data,who=who)

    def send(self,body='original fixture',who='alice',**data):
        return self.api('POST','/api/messages',dict(body=body,**data),who=who)['id']

    def message(self,mid,who='alice'):return self.api('GET',f'/api/messages/{mid}',who=who)['message']
    def edit(self,mid,body,revision=0,**kwargs):return self.api('POST',f'/api/messages/{mid}/edit',{'body':body,'revision':revision},**kwargs)
    def delete(self,mid,revision=0,**kwargs):return self.api('POST',f'/api/messages/{mid}/delete',{'revision':revision},**kwargs)

    def error(self,code,method,path,data=None,**kwargs):
        with self.assertRaises(urllib.error.HTTPError) as raised:self.api(method,path,data,**kwargs)
        self.assertEqual(raised.exception.code,code,raised.exception.read().decode())

    def test_old_own_message_keeps_identity_files_replies_and_receipts(self):
        mid=self.send(attachments=[{'name':'fixture.txt','content_b64':base64.b64encode(b'fixture file').decode()}])
        reply=self.send('agent reply',who='alice-agent',reply_to=mid)
        before=self.message(mid)
        with self.s.DB_LOCK:self.s.db().execute('UPDATE messages SET ts=1 WHERE id=?',(mid,))
        self.api('POST','/api/receipts',{'kind':'ack','ids':[mid]},who='alice-agent')
        changed=self.edit(mid,'edited **fixture**')['message']
        self.assertEqual(changed['id'],mid);self.assertEqual(changed['ts'],1)
        self.assertEqual(changed['author_id'],'alice');self.assertEqual(changed['thread_id'],before['thread_id'])
        self.assertEqual(changed['attachments'],before['attachments']);self.assertEqual(changed['revision'],1)
        self.assertTrue(changed['edited']);self.assertTrue(self.message(mid,'alice-agent')['acked'])
        self.assertEqual(self.message(reply)['reply_to'],mid)
        self.assertEqual(self.edit(mid,'edited **fixture**',revision=0)['message']['revision'],1)
        aid=changed['attachments'][0]['id']
        with self.fx.request('GET',f'/api/attachments/{aid}',who='alice') as response:self.assertEqual(response.read(),b'fixture file')

    def test_members_admin_agent_owner_and_agent_cannot_change_anothers_message(self):
        mid=self.send(who='bob')
        for who in ('alice','alice-agent','bob-agent','carol'):
            code=404 if who=='carol' else 403
            self.error(code,'POST',f'/api/messages/{mid}/edit',{'body':'forged','revision':0},who=who)
            self.error(code,'POST',f'/api/messages/{mid}/delete',{'revision':0},who=who)
        agent=self.send(who='alice-agent')
        for who in ('alice','alice-agent'):
            self.error(403,'POST',f'/api/messages/{agent}/edit',{'body':'forged','revision':0},who=who)
        self.assertEqual(self.message(mid)['body'],'original fixture')

    def test_validation_conflicts_and_cross_project_requests(self):
        mid=self.send()
        for body in ('', ' \n', None, [], 'x'*(self.s.MAX_BODY_CHARS+1), '\ud800', 'ghp_'+'a'*36):
            code=413 if isinstance(body,str) and len(body)>self.s.MAX_BODY_CHARS else 422 if isinstance(body,str) and body.startswith('ghp_') else 400
            self.error(code,'POST',f'/api/messages/{mid}/edit',{'body':body,'revision':0})
        self.error(400,'POST',f'/api/messages/{mid}/edit',{'body':'new','revision':True})
        self.error(400,'POST',f'/api/messages/{mid}/edit',{'body':'new','revision':0,'author':'bob'})
        self.edit(mid,'new')
        self.error(409,'POST',f'/api/messages/{mid}/edit',{'body':'stale','revision':0})
        self.error(409,'POST',f'/api/messages/{mid}/delete',{'revision':0})
        p=self.api('POST','/api/projects',{'title':'Second fixture'})['project']
        try:self.error(404,'POST',f'/api/messages/{mid}/edit',{'body':'wrong scope','revision':1},project=p['slug'])
        finally:self.api('POST','/api/project/delete',{'confirm':p['slug']},project=p['slug'])

    def test_delete_hides_text_files_and_search_but_keeps_replies(self):
        mid=self.send(attachments=[{'name':'fixture.txt','content_b64':base64.b64encode(b'fixture file').decode()}])
        reply=self.send('reply survives',who='alice-agent',reply_to=mid)
        aid=self.message(mid)['attachments'][0]['id']
        self.delete(mid);self.delete(mid)
        removed=self.message(mid)
        self.assertTrue(removed['deleted']);self.assertEqual(removed['body'],'Сообщение удалено')
        self.assertFalse(removed['attachments']);self.assertFalse(removed['can_edit']);self.assertFalse(removed['can_delete'])
        self.assertEqual(self.message(reply)['reply_to'],mid)
        self.assertFalse(self.api('GET','/api/messages?q=original')['messages'])
        self.assertFalse(self.api('GET','/api/messages?for_me=1&unacked=1',who='alice-agent')['messages'])
        self.error(404,'GET',f'/api/attachments/{aid}')
        self.error(410,'POST',f'/api/messages/{mid}/edit',{'body':'restore','revision':1})
        self.error(410,'POST',f'/api/messages/{mid}/reaction',{'emoji':'👍'})

    def test_approval_is_bound_to_the_text_version(self):
        mid=self.send(type='decision',approver='bob',needs_approval=True)
        self.api('POST',f'/api/messages/{mid}/approval',{'decision':'approve'},who='bob')
        changed=self.edit(mid,'a different decision')['message']
        self.assertFalse(changed['approvals']);self.assertEqual(changed['status']['state'],'open')
        history=self.s.db().execute('SELECT * FROM approval_history WHERE message_id=?',(mid,)).fetchone()
        self.assertEqual(history['user'],'bob');self.assertEqual(history['revision'],0)
        self.assertEqual(history['body_hash'],hashlib.sha256(b'original fixture').hexdigest())
        self.error(409,'POST',f'/api/messages/{mid}/approval',{'decision':'approve'},who='bob')
        self.error(403,'POST',f'/api/messages/{mid}/approval',{'decision':'approve','revision':1},who='alice')
        result=self.api('POST',f'/api/messages/{mid}/approval',{'decision':'approve','revision':1},who='bob')
        self.error(403,'POST',f'/api/messages/{result["approval_message"]}/edit',{'body':'changed audit','revision':0},who='bob')
        self.delete(mid,revision=1)
        self.error(410,'POST',f'/api/messages/{mid}/approval',{'decision':'approve','revision':2},who='bob')

    def test_uploaded_file_cleanup_is_durable_and_scoped(self):
        uid=self.fx.upload(b'uploaded deletion fixture',who='alice')
        keep=self.fx.upload(b'other file remains',who='alice')
        mid=self.send(body='',attachments=[{'upload_id':uid}])
        other=self.send(body='',attachments=[{'upload_id':keep}])
        with mock.patch.object(self.s.os,'remove',side_effect=PermissionError('fixture locked file')):
            self.delete(mid)
        self.assertTrue(self.message(mid)['deleted'])
        self.assertTrue(self.s.db().execute('SELECT 1 FROM pending_file_deletions WHERE upload_id=?',(uid,)).fetchone())
        self.s.purge_deleted_files()
        self.assertFalse(__import__('os').path.exists(self.s.upload_path(uid)))
        self.assertTrue(__import__('os').path.exists(self.s.upload_path(keep)))
        self.assertTrue(self.message(other)['attachments'])

    def test_deleted_secret_cannot_be_fetched_but_durable_claim_can_confirm(self):
        cid=self.api('POST','/api/conversations',{'peer_id':'alice-agent'})['conversation']['id']
        mid=self.api('POST',f'/api/conversations/{cid}/secrets',{'value':'fixture secret value','idem_key':'fixture-secret-delete'})['id']
        self.error(400,'POST',f'/api/messages/{mid}/edit',{'body':'changed card','revision':0})
        claim=secrets.token_hex(32)
        self.api('POST',f'/api/messages/{mid}/secret',{'claim_key':claim},who='alice-agent')
        self.delete(mid)
        self.error(410,'POST',f'/api/messages/{mid}/secret',{'claim_key':claim},who='alice-agent')
        self.assertTrue(self.api('POST',f'/api/messages/{mid}/secret',{'claim_key':claim,'action':'consume'},who='alice-agent')['ok'])
        self.assertIsNone(self.s.db().execute('SELECT envelope FROM secret_envelopes WHERE message_id=?',(mid,)).fetchone()[0])

    def test_private_changes_wake_only_authorized_content_revision(self):
        cid=self.api('POST','/api/conversations',{'peer_id':'alice-agent'})['conversation']['id']
        mid=self.send(conversation_id=cid)
        peer=self.api('GET','/api/messages?conversation=all',who='alice-agent')
        other=self.api('GET','/api/messages?conversation=all',who='bob')
        path=f'/api/wait?after={peer["last_id"]}&conversation=all&for_me=0&timeout=2&content_revision={peer["content_revision"]}'
        with concurrent.futures.ThreadPoolExecutor() as pool:
            waiting=pool.submit(self.api,'GET',path,None,'alice-agent');time.sleep(.1)
            self.edit(mid,'private edited fixture');result=waiting.result(timeout=3)
        self.assertNotEqual(result['content_revision'],peer['content_revision']);self.assertFalse(result['messages'])
        self.assertEqual(self.api('GET','/api/messages?conversation=all',who='bob')['content_revision'],other['content_revision'])
        self.error(404,'POST',f'/api/messages/{mid}/edit',{'body':'intrusion','revision':1},who='bob')
        self.assertFalse(self.api('GET','/api/messages')['messages'])


if __name__=='__main__':unittest.main(verbosity=2)
