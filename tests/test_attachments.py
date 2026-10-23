"""Real HTTP transfers, including a 512 MiB file; isolated database and clients."""
import base64
import hashlib
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


class AttachmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='telegent_files_')
        cls.work = Path(cls.tmp.name)
        spec = importlib.util.spec_from_file_location('attachment_server', ROOT/'server.py')
        cls.s = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.s)
        cls.s.Cfg.db = str(cls.work/'test.db')
        cls.tokens = {name: 'tgt_'+secrets.token_hex(24) for name in ('alice', 'alice-agent', 'bob-agent', 'carol')}
        con = cls.s.db()
        for name, token in cls.tokens.items():
            con.execute('INSERT INTO users(name,kind,owner,token_hash,created_ts) VALUES (?,?,?,?,?)',
                        (name, 'agent' if name.endswith('agent') else 'human', 'alice', cls.s.token_hash(token), time.time()))
        cls.pid = cls.s.create_project_row(con, 'main', 'main', 'alice')
        for name in ('alice', 'alice-agent', 'bob-agent'):
            cls.s.add_member(con, cls.pid, name, 'owner' if name == 'alice' else 'agent', 'alice')
        other = cls.s.create_project_row(con, 'other', 'other', 'carol')
        cls.s.add_member(con, other, 'carol', 'owner', 'carol')
        class Quiet(cls.s.Handler):
            def log_message(self, *args): pass
        cls.http = cls.s.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        cls.http.daemon_threads = True
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True); cls.thread.start()
        cls.url = 'http://127.0.0.1:'+str(cls.http.server_port)
        (cls.work/'agent.token').write_text(cls.tokens['bob-agent'])
        cls.config = cls.work/'client.json'
        cls.config.write_text(json.dumps({'server': cls.url, 'token_file': str(cls.work/'agent.token'),
                                         'project': 'main', 'url_source': '', 'state_file': str(cls.work/'state.json')}))
        cls.env = {k:v for k,v in os.environ.items() if not k.startswith('TELEGENT_')}
        cls.env.update(HOME=str(cls.work), USERPROFILE=str(cls.work), PYTHONIOENCODING='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); cls.thread.join(timeout=5)
        cls.s._db.close(); cls.tmp.cleanup()

    def request(self, method, path, data=None, who='bob-agent', headers=None, opener=None):
        body = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else None
        hdr = {'Content-Type': 'application/octet-stream' if isinstance(data, bytes) else 'application/json'}
        if who: hdr['Authorization'] = 'Bearer '+self.tokens[who]
        hdr.update(headers or {})
        req = urllib.request.Request(self.url+path, data=body, method=method, headers=hdr)
        return (opener.open if opener else urllib.request.urlopen)(req, timeout=180)

    def api(self, method, path, data=None, **kw):
        with self.request(method, path, data, **kw) as response: return json.load(response)

    def upload(self, raw, name='report.csv', who='bob-agent'):
        result = self.api('POST', '/api/uploads', {'name': name, 'size': len(raw), 'md5': hashlib.md5(raw).hexdigest()}, who=who)
        uid = result['id']
        for offset in range(0, len(raw), self.s.UPLOAD_CHUNK):
            self.api('POST', f'/api/uploads/{uid}/chunk?offset={offset}', raw[offset:offset+self.s.UPLOAD_CHUNK], who=who)
        self.api('POST', f'/api/uploads/{uid}/complete', {}, who=who)
        return uid

    def send(self, uid, key=None, who='bob-agent'):
        return self.api('POST', '/api/messages', {'attachments': [{'upload_id': uid}], 'idem_key': key}, who=who)

    def error(self, code, method, path, data=None, **kw):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.api(method, path, data, **kw)
        self.assertEqual(error.exception.code, code)
        return json.loads(error.exception.read())

    def cli(self, *args):
        return subprocess.run([sys.executable, '-X', 'utf8', str(ROOT/'tg.py'), '--config', str(self.config), *args],
                              env=self.env, cwd=self.work, capture_output=True, text=True, encoding='utf-8', timeout=180)

    def test_file_only_messages_legacy_files_and_browser_download(self):
        payload = b'fixture data\n'
        uid = self.upload(payload, name='../report.csv')
        mid = self.send(uid, 'fixture-key')['id']
        duplicate = self.send(uid, 'fixture-key')
        self.assertEqual(duplicate['id'], mid); self.assertTrue(duplicate['duplicate'])
        a = self.api('GET', f'/api/messages/{mid}')['message']['attachments'][0]
        self.assertEqual(a['name'], 'report.csv'); self.assertTrue(a['inline'])
        with self.request('GET', f"/api/attachments/{a['id']}", headers={'Range':'bytes=2-5'}) as r:
            self.assertEqual(r.status, 206); self.assertEqual(r.read(), payload[2:6])
        self.error(404, 'GET', f"/api/attachments/{a['id']}", who='carol')
        self.error(401, 'GET', f"/api/downloads/{a['id']}", who=None)
        jar = http.cookiejar.CookieJar(); browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        ticket = self.api('POST', f"/api/attachments/{a['id']}/download", {}, who='alice', opener=browser)
        self.assertEqual(ticket['url'], f"/api/downloads/{a['id']}")
        with self.request('GET', ticket['url'], who=None, opener=browser) as r: self.assertEqual(r.read(), payload)
        legacy = self.api('POST', '/api/messages', {'attachments':[{'name':'old.txt','content_b64':base64.b64encode(payload).decode()}]})['id']
        aid = self.api('GET', f'/api/messages/{legacy}')['message']['attachments'][0]['id']
        with self.request('GET', f'/api/attachments/{aid}') as r: self.assertEqual(r.read(), payload)

    def test_upload_resume_ownership_and_wrong_offsets(self):
        raw = b'chunk fixture'
        create = {'name': 'resume.bin', 'size': len(raw), 'key': 'resume-fixture'}
        uid = self.api('POST','/api/uploads', create)['id']
        self.error(404,'POST',f'/api/uploads/{uid}/complete',{},who='alice-agent')
        self.error(409,'POST',f'/api/uploads/{uid}/chunk?offset=1',raw)
        self.api('POST',f'/api/uploads/{uid}/chunk?offset=0',raw)
        self.api('POST',f'/api/uploads/{uid}/chunk?offset=0',raw)
        self.assertEqual(self.api('POST','/api/uploads',create)['received'],len(raw))
        self.error(409,'POST',f'/api/uploads/{uid}/chunk?offset=0',b'x'*len(raw))
        self.api('POST',f'/api/uploads/{uid}/complete',{})
        self.send(uid)
        self.error(409,'POST',f'/api/uploads/{uid}/cancel',{})
        self.error(409,'POST','/api/messages',{'attachments':[{'upload_id':uid}]})

    def test_low_disk_blocks_start_chunk_adoption_and_legacy_but_not_text(self):
        uid = self.upload(b'fixture')
        partial = self.api('POST','/api/uploads',{'name':'pending.txt','size':1})['id']
        low = types.SimpleNamespace(free=self.s.MIN_FREE_BYTES-1)
        before = self.api('GET','/api/messages')['last_id']
        with mock.patch.object(self.s.shutil,'disk_usage',return_value=low):
            self.assertFalse(self.api('GET','/api/storage')['can_upload'])
            for path, data in [('/api/uploads',{'name':'new.txt','size':1}),
                               (f'/api/uploads/{partial}/chunk?offset=0',b'x'),
                               ('/api/messages',{'attachments':[{'upload_id':uid}]}),
                               ('/api/messages',{'body':'caption','attachments':[{'name':'old.txt','content_b64':'eA=='}]})]:
                refusal = self.error(507,'POST',path,data)
                self.assertIn('300',refusal['error'])
            self.assertEqual(self.api('GET','/api/messages')['last_id'],before)
            self.api('POST','/api/messages',{'body':'text still works'})
            source=self.work/'low-space.txt';source.write_text('fixture')
            result=self.cli('send','-a',str(source))
            self.assertEqual(result.returncode,2);self.assertIn('300',result.stderr)
        self.send(uid)

    def test_limits_empty_files_and_secrets_across_parts(self):
        self.error(413,'POST','/api/uploads',{'name':'large.bin','size':self.s.MAX_FILE_BYTES+1})
        source=self.work/'oversize.bin'
        with source.open('wb') as stream:stream.truncate(self.s.MAX_FILE_BYTES+1)
        result=self.cli('send','-a',str(source))
        self.assertEqual(result.returncode,2);self.assertIn('512',result.stderr)
        empty=self.upload(b'',name='empty.txt');self.send(empty)
        # Split a secret marker across the two HTTP requests.
        raw = bytes(256*1024-10) + b'-----BEGIN PRIVATE KEY-----'
        uid=self.api('POST','/api/uploads',{'name':'blocked.txt','size':len(raw)})['id']
        self.api('POST',f'/api/uploads/{uid}/chunk?offset=0',raw[:20])
        self.error(422,'POST',f'/api/uploads/{uid}/chunk?offset=20',raw[20:])
        self.assertFalse(Path(self.s.upload_path(uid)).exists())
        self.error(404,'POST','/api/messages',{'attachments':[{'upload_id':uid}]})

    def test_agent_upload_download_and_preserve_existing_file_on_corruption(self):
        source=self.work/'agent-report.bin';source.write_bytes(b'fixture\x00'*800000)
        result=self.cli('send','-a',str(source),'--key','agent-file-fixture')
        self.assertEqual(result.returncode,0,result.stderr)
        mid=int(result.stdout.split()[0][1:]);out=self.work/'download'
        result=self.cli('get',str(mid),'--out',str(out))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((out/source.name).read_bytes(),source.read_bytes())
        self.assertIn('md5 ok',result.stdout)
        again_download=self.cli('get',str(mid),'--out',str(out))
        self.assertEqual(again_download.returncode,0,again_download.stderr)
        self.assertEqual((out/(source.stem+' (1)'+source.suffix)).read_bytes(),source.read_bytes())
        again=self.cli('send','-a',str(source),'--key','agent-file-fixture')
        self.assertEqual(again.returncode,0,again.stderr);self.assertIn('дубль не создан',again.stdout)
        aid=self.api('GET',f'/api/messages/{mid}')['message']['attachments'][0]['id']
        with self.s.DB_LOCK:self.s.db().execute('UPDATE attachments SET md5=? WHERE id=?',('0'*32,aid))
        (out/source.name).write_bytes(b'keep existing')
        result=self.cli('get',str(mid),'--out',str(out))
        self.assertEqual(result.returncode,2);self.assertEqual((out/source.name).read_bytes(),b'keep existing')

    def test_real_512_mib_transfer_and_oversize_rejection(self):
        size=self.s.MAX_FILE_BYTES;chunk=bytes(self.s.UPLOAD_CHUNK)
        uid=self.api('POST','/api/uploads',{'name':'boundary.bin','size':size})['id']
        digest=hashlib.md5()
        for offset in range(0,size,len(chunk)):
            self.api('POST',f'/api/uploads/{uid}/chunk?offset={offset}',chunk)
            digest.update(chunk)
        self.assertEqual(self.api('POST',f'/api/uploads/{uid}/complete',{})['md5'],digest.hexdigest())
        mid=self.send(uid)['id'];out=self.work/'boundary-download'
        result=self.cli('get',str(mid),'--out',str(out))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((out/'boundary.bin').stat().st_size,size)
        self.assertIn('md5 ok',result.stdout)

    def test_native_download_ticket_expires_on_logout(self):
        uid=self.upload(b'private fixture');mid=self.send(uid)['id']
        aid=self.api('GET',f'/api/messages/{mid}')['message']['attachments'][0]['id']
        with self.s.DB_LOCK: session=self.s.new_session('alice')
        headers={'Authorization':'Bearer '+session}
        jar=http.cookiejar.CookieJar();browser=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        ticket=self.api('POST',f'/api/attachments/{aid}/download',{},headers=headers,opener=browser)
        self.api('POST','/api/logout',{},headers=headers)
        self.error(401,'GET',ticket['url'],who=None,opener=browser)

    def test_cancel_expiry_restart_and_project_deletion_preserve_other_files(self):
        abandoned=self.api('POST','/api/uploads',{'name':'draft.bin','size':0})['id']
        keep=self.upload(b'preserved fixture');mid=self.send(keep)['id']
        with self.s.DB_LOCK:
            self.s.db().execute('UPDATE file_uploads SET updated=? WHERE id IN (?,?)',(time.time()-90000,keep,abandoned))
        cancelled=self.api('POST','/api/uploads',{'name':'cancel.bin','size':0})['id']
        self.assertFalse(Path(self.s.upload_path(abandoned)).exists())
        self.assertTrue(Path(self.s.upload_path(keep)).exists())
        self.api('POST',f'/api/uploads/{cancelled}/cancel',{})
        self.assertFalse(Path(self.s.upload_path(cancelled)).exists())
        with self.s.DB_LOCK:
            self.s._db.close();self.s._db=None;self.s.db()
        aid=self.api('GET',f'/api/messages/{mid}')['message']['attachments'][0]['id']
        with self.request('GET',f'/api/attachments/{aid}') as r:self.assertEqual(r.read(),b'preserved fixture')
        slug=self.api('POST','/api/projects',{'title':'Cleanup team'},who='alice')['project']['slug']
        uid=self.api('POST','/api/uploads?project='+slug,{'name':'empty.bin','size':0},who='alice')['id']
        self.api('POST',f'/api/uploads/{uid}/complete?project='+slug,{},who='alice')
        self.api('POST','/api/messages?project='+slug,{'attachments':[{'upload_id':uid}]},who='alice')
        self.api('POST','/api/project/delete?project='+slug,{'confirm':slug},who='alice')
        self.assertFalse(Path(self.s.upload_path(uid)).exists())
        self.assertTrue(Path(self.s.upload_path(keep)).exists())


if __name__ == '__main__': unittest.main(verbosity=2)
