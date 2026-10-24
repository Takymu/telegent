"""Private-message boundaries, real CLI secret delivery, and authenticated encryption."""
import base64
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import unittest
import urllib.error

import test_attachments as fixture


class DirectMessageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest();cls.s=cls.fx.s
        cls.fx.tokens['bob']='tgt_'+secrets.token_hex(24)
        cls.s.db().execute('INSERT INTO users(name,kind,owner,token_hash,created_ts) VALUES (?,?,?,?,?)',('bob','human','bob',cls.s.token_hash(cls.fx.tokens['bob']),time.time()))
        cls.s.add_member(cls.s.db(),cls.fx.pid,'bob','member','alice')
        cls.s.db().execute("UPDATE users SET is_admin=1 WHERE name='alice'")

    @classmethod
    def tearDownClass(cls):fixture.AttachmentTest.tearDownClass()

    def setUp(self):
        with self.s.DB_LOCK:
            for table in ('secret_envelopes','conversations','attachments','receipts','approvals','reactions','messages'):
                self.s.db().execute('DELETE FROM '+table)
        self.cid=self.api('POST','/api/conversations',{'peer_id':'alice-agent'},'bob')['conversation']['id']

    def api(self,method,path,data=None,who='bob'):
        return self.fx.api(method,path+('&' if '?' in path else '?')+'project=main',data,who=who)

    def deny(self,code,method,path,data=None,who='alice'):
        with self.assertRaises(urllib.error.HTTPError) as raised:self.api(method,path,data,who)
        self.assertEqual(raised.exception.code,code)

    def send(self,body='fixture private text',**data):
        return self.api('POST','/api/messages',dict(body=body,conversation_id=self.cid,**data))['id']

    def secret(self,**data):
        return self.api('POST',f'/api/conversations/{self.cid}/secrets',dict({'value':'ghp_'+'a'*36,'idem_key':secrets.token_hex(16)},**data))['id']

    def test_private_feed_is_visible_only_to_its_two_parties(self):
        mid=self.send()
        self.assertEqual(self.api('GET','/api/messages')['messages'],[])
        self.assertEqual([m['id'] for m in self.api('GET','/api/messages?conversation='+self.cid)['messages']],[mid])
        self.assertEqual([m['id'] for m in self.api('GET','/api/messages?for_me=1',who='alice-agent')['messages']],[mid])
        for who in ('alice','bob-agent','carol'):
            self.deny(404,'GET',f'/api/messages/{mid}',who=who)
            self.assertNotIn(mid,[m['id'] for m in self.api('GET','/api/messages?conversation=all&q=fixture',who=who)['messages']] if who!='carol' else [])
        self.assertEqual(self.api('GET','/api/conversations',who='alice')['conversations'],[])
        self.assertNotIn('fixture private text',json.dumps(self.api('GET','/api/summary',who='alice')))

    def test_agent_reply_and_status_stay_private_and_cannot_add_a_third_person(self):
        mid=self.send(type='request')
        reply=self.api('POST','/api/messages',{'body':'private reply','reply_to':mid},'alice-agent')['id']
        thread=self.api('GET',f'/api/messages/{mid}')['thread']
        self.assertEqual({m['id'] for m in thread},{mid,reply});self.assertTrue(all(m['conversation_id']==self.cid for m in thread))
        notification=self.api('POST',f'/api/messages/{mid}/status',{'state':'taken'},'alice-agent')['status_message']
        self.deny(404,'GET',f'/api/messages/{notification}')
        self.deny(400,'POST','/api/messages',{'body':'bad','conversation_id':self.cid,'to_ids':['alice']},'bob')
        self.deny(400,'POST','/api/messages',{'body':'bad','conversation_id':self.cid,'approver':'alice'},'bob')
        self.deny(409,'POST','/api/messages',{'body':'bad','reply_to':mid,'conversation_id':None},'alice-agent')

    def test_attachments_reactions_receipts_and_guessed_ids_keep_the_same_boundary(self):
        mid=self.send(attachments=[{'name':'fixture.txt','content_b64':base64.b64encode(b'private attachment').decode()}])
        aid=self.api('GET',f'/api/messages/{mid}')['message']['attachments'][0]['id']
        for who in ('alice','bob-agent'):
            self.deny(404,'GET',f'/api/attachments/{aid}',who=who)
            self.deny(404,'POST',f'/api/attachments/{aid}/download',{},who)
            self.deny(404,'POST',f'/api/messages/{mid}/reaction',{'emoji':'+1'},who)
            self.deny(404,'POST','/api/messages',{'body':'bad','reply_to':mid},who)
            self.deny(404,'POST','/api/messages',{'body':'bad','conversation_id':self.cid},who)
            self.assertEqual(self.api('POST','/api/receipts',{'kind':'ack','ids':[mid]},who)['ids'],[])
        self.assertEqual(self.api('POST',f'/api/messages/{mid}/reaction',{'emoji':'+1'},'alice-agent')['reactions'][0]['count'],1)

    def test_project_bound_identity_and_removal_revoke_old_conversations(self):
        self.deny(404,'POST','/api/conversations',{'peer_id':'carol'},'alice-agent')
        self.deny(400,'POST','/api/conversations',{'peer_id':'bob-agent'},'alice-agent')
        mid=self.send()
        self.api('POST','/api/members/remove',{'account_id':'bob','name':'bob'},'alice')
        self.deny(404,'GET',f'/api/messages/{mid}',who='alice-agent')
        self.assertEqual(self.api('GET','/api/messages?for_me=1',who='alice-agent')['messages'],[])
        self.s.add_member(self.s.db(),self.fx.pid,'bob','member','alice')

    def test_private_read_cursor_is_separate_and_monotonic(self):
        first=self.send()
        self.api('POST','/api/receipts?conversation='+self.cid,{'kind':'read','seen_upto':first+10000},'alice-agent')
        self.assertEqual(self.api('GET','/api/conversations',who='alice-agent')['conversations'][0]['unread'],0)
        second=self.send('new fixture')
        self.assertEqual(self.api('GET','/api/conversations',who='alice-agent')['conversations'][0]['unread'],1)
        self.assertGreater(second,first)
        self.deny(404,'POST','/api/receipts?conversation='+self.cid,{'seen_upto':second},'alice')

    def test_idempotence_cannot_move_a_message_between_public_and_private(self):
        key=secrets.token_hex(20);mid=self.send(idem_key=key)
        self.assertEqual(self.send(idem_key=key),mid)
        self.deny(409,'POST','/api/messages',{'body':'bad','idem_key':key},'bob')

    def test_secret_is_encrypted_at_rest_never_in_message_api_and_operator_can_decrypt(self):
        mid=self.secret();row=self.s.db().execute('SELECT * FROM secret_envelopes WHERE message_id=?',(mid,)).fetchone()
        value='ghp_'+'a'*36
        self.assertNotIn(value,row['envelope'])
        message=self.api('GET',f'/api/messages/{mid}')['message']
        self.assertNotIn(value,json.dumps(message));self.assertEqual(message['secret']['state'],'pending')
        self.assertEqual(message['attachments'],[])
        self.assertEqual(self.api('GET','/api/messages?conversation=all&q='+value)['messages'],[])
        ordinary=self.send(value)
        self.assertEqual(self.api('GET',f'/api/messages/{ordinary}')['message']['body'],value)
        self.deny(404,'GET',f'/api/messages/{ordinary}',who='alice')
        envelope=json.loads(row['envelope']);m=self.s.get_message_row(mid)
        aad=json.dumps(['telegent-secret-v1',self.cid,'bob','alice-agent',m['idem_key']],ensure_ascii=False,separators=(',',':')).encode()
        self.assertEqual(self.s.unseal_secret(envelope,aad),value.encode())
        with self.assertRaises(self.s.ApiError):self.s.unseal_secret(envelope,aad+b'changed')

    def test_secret_download_reservation_retry_consume_and_access(self):
        mid=self.secret();key=secrets.token_hex(32)
        for who in ('alice','bob-agent'):self.deny(404,'POST',f'/api/messages/{mid}/secret',{'claim_key':key},who)
        self.deny(403,'POST',f'/api/messages/{mid}/secret',{'claim_key':key},'bob')
        received=self.api('POST',f'/api/messages/{mid}/secret',{'claim_key':key},'alice-agent')
        self.assertEqual(base64.b64decode(received['content_b64']),('ghp_'+'a'*36).encode())
        self.assertEqual(self.api('POST',f'/api/messages/{mid}/secret',{'claim_key':key},'alice-agent'),received)
        self.deny(409,'POST',f'/api/messages/{mid}/secret',{'claim_key':secrets.token_hex(32)},'alice-agent')
        data={'action':'consume','claim_key':key}
        self.api('POST',f'/api/messages/{mid}/secret',data,'alice-agent');self.api('POST',f'/api/messages/{mid}/secret',data,'alice-agent')
        self.assertIsNone(self.s.db().execute('SELECT envelope FROM secret_envelopes WHERE message_id=?',(mid,)).fetchone()[0])
        self.deny(410,'POST',f'/api/messages/{mid}/secret',{'claim_key':key},'alice-agent')

    def test_expiry_revoke_and_idempotent_send_do_not_revive_a_secret(self):
        key=secrets.token_hex(20);mid=self.secret(idem_key=key)
        self.assertEqual(self.secret(idem_key=key),mid)
        self.api('POST',f'/api/messages/{mid}/secret',{'action':'revoke'})
        self.assertEqual(self.secret(idem_key=key),mid)
        self.deny(410,'POST',f'/api/messages/{mid}/secret',{'claim_key':secrets.token_hex(32)},'alice-agent')
        expired=self.secret();self.s.db().execute('UPDATE secret_envelopes SET expires=? WHERE message_id=?',(time.time()-1,expired))
        self.s.purge_expired_secrets()
        self.assertEqual(self.api('GET',f'/api/messages/{expired}')['message']['secret']['state'],'expired')
        self.assertIsNone(self.s.db().execute('SELECT envelope FROM secret_envelopes WHERE message_id=?',(expired,)).fetchone()[0])

    def test_real_client_private_reply_and_secret_file_with_no_value_in_stdout(self):
        mid=self.secret();cfg=self.fx.work/'private-client.json';tok=self.fx.work/'private-agent.token';tok.write_text(self.fx.tokens['alice-agent'])
        cfg.write_text(json.dumps({'server':self.fx.url,'token_file':str(tok),'project':'main','url_source':'','state_file':str(self.fx.work/'private-state.json')}))
        def cli(*args):
            return subprocess.run([sys.executable,str(fixture.ROOT/'tg.py'),'--config',str(cfg),*args],env=self.fx.env,capture_output=True,text=True,timeout=15)
        out=self.fx.work/('secret-'+str(mid)+'.txt')
        result=cli('secret',str(mid),'--out',str(out));self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(out.read_text(),'ghp_'+'a'*36);self.assertNotIn(out.read_text(),result.stdout+result.stderr)
        retry=cli('secret',str(mid),'--out',str(out));self.assertEqual(retry.returncode,0,retry.stderr)
        result=cli('reply',str(mid),'private fixture reply');self.assertEqual(result.returncode,0,result.stderr)
        result=cli('dm','--to','bob','private fixture direct command');self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.api('GET','/api/messages')['messages'],[])
        inbox=cli('inbox','--full');self.assertEqual(inbox.returncode,0,inbox.stderr);self.assertIn('ЛИЧНОЕ',inbox.stdout);self.assertNotIn(out.read_text(),inbox.stdout)

    def test_aes_gcm_matches_standard_vector_and_rejects_modified_tag(self):
        # NIST AES-256-GCM: empty AAD and a single all-zero block.
        c=self.s.SecretCipher();encrypted,tag=c.crypt(bytes(32),bytes(12),bytes(16),b'')
        self.assertEqual(encrypted.hex(),'cea7403d4d606b6e074ec5d3baf39d18')
        self.assertEqual(tag.hex(),'d0d1c8a799996bf0265b98b5d48ab919')
        with self.assertRaises(self.s.ApiError):c.crypt(bytes(32),bytes(12),encrypted,b'',bytes(16))

    def test_private_delivery_and_hook_metadata_do_not_reach_another_agent(self):
        last=self.api('GET','/api/messages')['last_id']
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending=pool.submit(self.api,'GET',f'/api/wait?after={last}&timeout=2',None,'alice-agent')
            time.sleep(.1);mid=self.send('fixture confidential notice')
            self.assertEqual([m['id'] for m in pending.result(timeout=3)['messages']],[mid])
        summary=self.api('GET','/api/summary?metadata=1',who='alice-agent')
        self.assertIn(mid,[m['id'] for m in summary['unacked']])
        self.assertNotIn('fixture confidential notice',json.dumps(summary))
        other=self.api('GET',f'/api/wait?after={last}&timeout=0.1',who='bob-agent')
        self.assertEqual(other['messages'],[])
        self.assertNotIn(mid,[m['id'] for m in self.api('GET','/api/summary?metadata=1',who='bob-agent')['unacked']])

    def test_lost_server_key_fails_closed_and_never_replaces_existing_encrypted_packages(self):
        mid=self.secret();path=Path(str(self.s.Cfg.db)+'.secret-key');saved=path.read_bytes();path.unlink()
        try:
            self.deny(503,'POST',f'/api/messages/{mid}/secret',{'claim_key':secrets.token_hex(32)},'alice-agent')
            self.deny(503,'POST',f'/api/conversations/{self.cid}/secrets',{'value':'fixture','idem_key':secrets.token_hex(16)},'bob')
            self.assertFalse(path.exists())
            self.assertIsNotNone(self.s.db().execute('SELECT envelope FROM secret_envelopes WHERE message_id=?',(mid,)).fetchone()[0])
        finally:path.write_bytes(saved)

    def test_secret_receive_saves_value_without_mutating_client_configuration(self):
        value='fixture credential value without a token prefix'
        mid=self.secret(value=value);cfg=self.fx.work/'literal-client.json';tok=self.fx.work/'literal-agent.token';tok.write_text(self.fx.tokens['alice-agent'])
        cfg.write_text(json.dumps({'server':self.fx.url,'token_file':str(tok),'project':'main','url_source':''}))
        out=self.fx.work/('literal-'+str(mid)+'.txt')
        base=[sys.executable,str(fixture.ROOT/'tg.py'),'--config',str(cfg)]
        original=cfg.read_bytes()
        result=subprocess.run(base+['secret',str(mid),'--out',str(out)],env=self.fx.env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(cfg.read_bytes(),original)
        self.assertEqual(out.read_text(),value)
        self.assertNotIn(value,result.stdout+result.stderr)
        result=subprocess.run(base+['send',value],env=self.fx.env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_claimed_value_cannot_be_recalled_but_late_save_confirmation_is_safe(self):
        mid=self.secret();key=secrets.token_hex(32)
        self.api('POST',f'/api/messages/{mid}/secret',{'claim_key':key},'alice-agent')
        self.deny(409,'POST',f'/api/messages/{mid}/secret',{'action':'revoke'},'bob')
        self.s.db().execute('UPDATE secret_envelopes SET expires=? WHERE message_id=?',(time.time()-1,mid))
        self.s.purge_expired_secrets()
        self.api('POST',f'/api/messages/{mid}/secret',{'action':'consume','claim_key':key},'alice-agent')
        self.assertEqual(self.api('GET',f'/api/messages/{mid}')['message']['secret']['state'],'received')


if __name__=='__main__':unittest.main(verbosity=2)
