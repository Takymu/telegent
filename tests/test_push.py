"""Durable native push enrollment, current access and bounded delivery retries."""
import json
import io
import base64
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import unittest
import urllib.error
from unittest import mock

import test_attachments as fixture


class PushTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest();cls.s=cls.fx.s
        account=cls.fx.work/'push-account.json';account.write_text('{}')
        cls.s.Cfg.push={'service_account_file':str(account),'android':{
            'app_id':'fixture-app','api_key':'fixture-api','project_id':'fixture-project','sender_id':'123456'}}

    @classmethod
    def tearDownClass(cls):fixture.AttachmentTest.tearDownClass()

    def setUp(self):
        with self.s.DB_LOCK:
            self.s.db().execute('DELETE FROM push_outbox');self.s.db().execute('DELETE FROM push_devices')
            self.s.db().execute("UPDATE users SET revoked=0 WHERE name='alice'")
        self.key=secrets.token_hex(32);self.token='fixture-'+secrets.token_hex(24);self.binding=secrets.token_hex(16)

    def register(self,who='alice',key=None,token=None):
        return self.fx.api('POST','/api/push/devices',{'device_key':key or self.key,'fcm_token':token or self.token,'binding':self.binding},who=who)

    def send(self,**kw):
        return self.fx.api('POST','/api/messages',{'body':'PRIVATE_CONTENT_DO_NOT_PUSH','to_ids':['alice'],**kw})['id']

    def jobs(self):return self.s.db().execute('SELECT * FROM push_outbox').fetchall()

    def test_enrollment_requires_person_binds_self_and_never_replays_history(self):
        configuration=self.fx.api('GET','/api/push/config',who='alice')
        self.assertTrue(configuration['configured']);self.assertEqual(set(configuration['firebase']),{'app_id','api_key','project_id','sender_id'})
        self.assertNotIn('service_account_file',repr(configuration))
        old=self.send()
        self.fx.error(403,'POST','/api/push/devices',{'device_key':self.key,'fcm_token':self.token})
        registered=self.register();self.assertTrue(registered['server_id']);self.assertEqual(self.jobs(),[])
        self.register();self.assertEqual(self.s.db().execute('SELECT COUNT(*) FROM push_devices').fetchone()[0],1)
        self.fx.error(409,'POST','/api/push/devices',{'device_key':secrets.token_hex(32),'fcm_token':self.token,'binding':self.binding},who='carol')
        mid=self.send(idem_key='push-'+self.key);self.send(idem_key='push-'+self.key)
        self.assertEqual([r['message_id'] for r in self.jobs()],[mid]);self.assertNotEqual(old,mid)

    def test_visibility_and_account_read_state_are_rechecked_at_send(self):
        self.register();self.register(who='carol',key=secrets.token_hex(32),token='other-'+secrets.token_hex(24))
        mid=self.send();self.assertEqual(len(self.jobs()),1)
        self.fx.api('GET',f'/api/messages/{mid}?mark=read',who='alice')
        sent=[];self.s.dispatch_push(lambda *args:sent.append(args),time.time()+10)
        self.assertEqual(sent,[]);self.assertEqual(self.jobs(),[])
        self.fx.api('POST','/api/messages?project=other',{'body':'foreign','priority':'urgent'},who='carol')
        self.assertEqual(self.jobs(),[])

    def test_private_messages_keep_their_pair_and_generic_provider_payload(self):
        self.register()
        cid=self.fx.api('POST','/api/conversations',{'peer_id':'bob-agent'},who='alice')['conversation']['id']
        mid=self.send(conversation_id=cid)
        sender=self.s.FcmSender();captured=[]
        with mock.patch.object(sender,'authorization',return_value='fixture-access'),mock.patch.object(sender,'post',side_effect=lambda *a:captured.append(a)):
            self.s.dispatch_push(sender,time.time()+10)
        self.assertEqual(len(captured),1)
        payload=json.loads(captured[0][1])['message']
        self.assertEqual(payload['data']['message_id'],str(mid));self.assertEqual(payload['token'],self.token)
        self.assertEqual(set(payload['data']),{'message_id','server_id'})
        self.assertNotIn('PRIVATE_CONTENT_DO_NOT_PUSH',repr(captured))
        self.assertNotIn(cid,repr(captured));self.assertNotIn('notification',payload)

    def test_retries_survive_restart_token_rotation_and_have_no_chat_duplicates(self):
        self.register();mid=self.send();now=time.time()+10
        def fail(*args):raise self.s.PushFailure()
        self.s.dispatch_push(fail,now);self.assertEqual(self.jobs()[0]['attempts'],1)
        with self.s.DB_LOCK:self.s._db.close();self.s._db=None;self.s.db()
        self.assertEqual(self.jobs()[0]['message_id'],mid)
        new_token='rotated-'+secrets.token_hex(24)
        self.fx.api('POST','/api/push/devices/refresh',{'device_key':self.key,'fcm_token':new_token,'binding':self.binding},who=None)
        sent=[];self.s.dispatch_push(lambda *a:sent.append(a),now+100)
        self.assertEqual(sent[0][0],new_token);self.assertEqual(self.jobs(),[])

    def test_revocation_removal_and_wrong_capability_stop_delivery(self):
        self.register();self.send()
        self.fx.error(410,'POST','/api/push/devices/refresh',{'device_key':secrets.token_hex(32),'fcm_token':self.token,'binding':self.binding},who=None)
        with self.s.DB_LOCK:self.s.db().execute("UPDATE users SET revoked=1 WHERE name='alice'")
        sent=[];self.s.dispatch_push(lambda *a:sent.append(a),time.time()+10)
        self.assertEqual(sent,[]);self.assertEqual(self.jobs(),[])
        self.fx.api('POST','/api/push/devices/remove',{'device_key':self.key,'binding':self.binding},who=None)
        self.assertEqual(self.s.db().execute('SELECT COUNT(*) FROM push_devices').fetchone()[0],0)

    def test_token_refresh_during_failed_send_retries_with_new_token(self):
        self.register();self.send();now=time.time()+10;rotated='rotation-'+secrets.token_hex(24)
        def rotate(*args):
            self.fx.api('POST','/api/push/devices/refresh',{'device_key':self.key,'fcm_token':rotated,'binding':self.binding},who=None)
            raise self.s.PushFailure(permanent=True,unregistered=True)
        self.s.dispatch_push(rotate,now);self.assertEqual(len(self.jobs()),1)
        sent=[];self.s.dispatch_push(lambda *a:sent.append(a),now+2)
        self.assertEqual(sent[0][0],rotated)

    def test_session_logout_and_membership_removal_cancel_pending_pushes(self):
        session='tgs_'+secrets.token_hex(24)
        with self.s.DB_LOCK:
            self.s.db().execute('INSERT INTO sessions(token_hash,user,created_ts) VALUES (?,?,?)',
                                (self.s.token_hash(session),'alice',time.time()))
        self.fx.api('POST','/api/push/devices',{'device_key':self.key,'fcm_token':self.token,'binding':self.binding},
                    who=None,headers={'Authorization':'Bearer '+session})
        self.send()
        self.fx.api('POST','/api/logout',{},who=None,headers={'Authorization':'Bearer '+session})
        sent=[];self.s.dispatch_push(lambda *a:sent.append(a),time.time()+10)
        self.assertEqual(sent,[]);self.assertEqual(self.jobs(),[])
        self.register();self.send()
        with self.s.DB_LOCK:self.s.db().execute('DELETE FROM members WHERE project_id=? AND user=?',(self.fx.pid,'alice'))
        try:
            self.s.dispatch_push(lambda *a:sent.append(a),time.time()+10)
            self.assertEqual(sent,[]);self.assertEqual(self.jobs(),[])
        finally:
            with self.s.DB_LOCK:self.s.add_member(self.s.db(),self.fx.pid,'alice','owner','alice')

    def test_device_rebinding_discards_old_account_queue_and_capability_cannot_read(self):
        self.register();self.send();self.assertEqual(len(self.jobs()),1)
        self.register(who='carol');self.assertEqual(self.jobs(),[])
        self.fx.error(401,'GET','/api/me',who=None,headers={'Authorization':'Bearer '+self.key})
        self.fx.error(400,'POST','/api/push/devices/refresh',[],who=None)
        self.fx.error(400,'POST','/api/push/devices',{'device_key':self.key,'fcm_token':'bad'},who='alice')
        self.fx.error(403,'GET','/api/push/config')

    def test_provider_rate_limit_and_unregistered_responses(self):
        self.register();mid=self.send();now=time.time()+10
        sender=self.s.FcmSender()
        response=urllib.error.HTTPError('https://fcm.googleapis.com/',429,'slow down',{'Retry-After':'120'},io.BytesIO(b'{}'))
        with mock.patch.object(self.s.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=response
            with self.assertRaises(self.s.PushFailure) as failure:sender.post('https://fcm.googleapis.com/',b'{}',{})
        self.assertEqual(failure.exception.retry_after,120)
        def retry(*args):raise failure.exception
        self.s.dispatch_push(retry,now)
        self.assertGreaterEqual(self.jobs()[0]['next_try'],now+120)
        response=urllib.error.HTTPError('https://fcm.googleapis.com/',404,'unregistered',{},
            io.BytesIO(json.dumps({'error':{'details':[{'errorCode':'UNREGISTERED'}]}}).encode()))
        with mock.patch.object(self.s.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=response
            def expired(*args):sender.post('https://fcm.googleapis.com/',b'{}',{})
            self.s.dispatch_push(expired,now+200)
        self.assertEqual(self.jobs(),[])
        self.assertEqual(self.s.db().execute('SELECT COUNT(*) FROM push_devices').fetchone()[0],0)

    def test_oauth_signs_real_jwt_caches_access_and_removes_temporary_key(self):
        openssl=shutil.which('openssl') or str(Path(os.environ.get('PROGRAMFILES','C:/Program Files'))/'Git/usr/bin/openssl.exe')
        if not Path(openssl).is_file():self.skipTest('OpenSSL is required for signing verification')
        key=self.fx.work/'fixture-rsa.pem';public=self.fx.work/'fixture-public.pem'
        subprocess.run([openssl,'genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(key)],capture_output=True,check=True)
        subprocess.run([openssl,'pkey','-in',str(key),'-pubout','-out',str(public)],capture_output=True,check=True)
        account=self.fx.work/'fixture-service.json'
        account.write_text(json.dumps({'project_id':'fixture-project','client_email':'fixture@example.invalid','private_key':key.read_text()}))
        prior=self.s.Cfg.push;self.s.Cfg.push=dict(prior,service_account_file=str(account));sender=self.s.FcmSender();calls=[];temporary=[]
        run=subprocess.run
        def signed(args,**kw):
            temporary.append(Path(args[-1]));return run(args,**kw)
        def oauth(url,body,headers):
            self.assertEqual(url,'https://oauth2.googleapis.com/token')
            jwt=self.s.urllib.parse.parse_qs(body.decode())['assertion'][0];header,claims,signature=jwt.split('.')
            decode=lambda value:base64.urlsafe_b64decode(value+'='*(-len(value)%4))
            self.assertEqual(json.loads(decode(header)),{'alg':'RS256','typ':'JWT'})
            payload=json.loads(decode(claims));self.assertEqual(payload['iss'],'fixture@example.invalid')
            self.assertEqual(payload['aud'],url);self.assertEqual(payload['exp']-payload['iat'],3600)
            signature_file=self.fx.work/'fixture-signature';signature_file.write_bytes(decode(signature))
            run([openssl,'dgst','-sha256','-verify',str(public),'-signature',str(signature_file)],input=(header+'.'+claims).encode(),capture_output=True,check=True)
            calls.append(jwt);return {'access_token':'fixture-access','expires_in':3600}
        try:
            with mock.patch.object(self.s.shutil,'which',return_value=openssl),mock.patch.object(self.s.subprocess,'run',side_effect=signed),mock.patch.object(sender,'post',side_effect=oauth):
                self.assertEqual(sender.authorization(),'fixture-access');self.assertEqual(sender.authorization(),'fixture-access')
            self.assertEqual(len(calls),1);self.assertTrue(temporary);self.assertTrue(all(not path.exists() for path in temporary))
        finally:self.s.Cfg.push=prior

    def test_delayed_disable_and_refresh_cannot_overwrite_a_new_enrollment(self):
        self.register();old=self.binding;self.binding=secrets.token_hex(16);self.register();mid=self.send()
        self.fx.api('POST','/api/push/devices/remove',{'device_key':self.key,'binding':old},who=None)
        self.fx.error(410,'POST','/api/push/devices/refresh',
                      {'device_key':self.key,'binding':old,'fcm_token':'old-'+secrets.token_hex(24)},who=None)
        self.assertEqual([r['message_id'] for r in self.jobs()],[mid])
        row=self.s.db().execute('SELECT * FROM push_devices').fetchone()
        self.assertEqual(row['binding'],self.binding);self.assertEqual(row['fcm_token'],self.token)

    def test_authenticated_provider_request_never_follows_redirects(self):
        received=[]
        @self.s.route('POST','/push-fixture-redirect',auth=False)
        def redirect(h,user,q,data):
            h.send_response(302);h.send_header('Location',self.fx.url+'/push-fixture-target');h.send_header('Content-Length','0');h.end_headers()
        @self.s.route('GET','/push-fixture-target',auth=False)
        def target(h,user,q,data):received.append(h.headers.get('Authorization'));return {'ok':True}
        try:
            with self.assertRaises(self.s.PushFailure):
                self.s.FcmSender().post(self.fx.url+'/push-fixture-redirect',b'{}',{'Authorization':'Bearer fixture-access'})
            self.assertEqual(received,[])
        finally:self.s.ROUTES[:]=[row for row in self.s.ROUTES if row[3] not in (redirect,target)]


if __name__=='__main__':unittest.main(verbosity=2)
