"""Fault injection: committed sends with lost responses, malformed replies and relocation."""
from contextlib import contextmanager
import http.server
import importlib.util
import json
import threading
import types
import unittest
from unittest import mock
import urllib.request
import uuid

import test_attachments as fixture

spec=importlib.util.spec_from_file_location('recovery_client',fixture.ROOT/'tg.py')
tg=importlib.util.module_from_spec(spec);spec.loader.exec_module(tg)


@contextmanager
def proxy(fx,mode):
    state={'calls':0,'target_calls':0}
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.serve()
        def do_POST(self):self.serve()
        def serve(self):
            state['calls']+=1
            raw=self.rfile.read(int(self.headers.get('Content-Length',0)))
            if self.path=='/target':state['target_calls']+=1
            if mode=='redirect' and self.path!='/target':
                self.send_response(302);self.send_header('Location',base+'/target');self.end_headers();return
            if mode=='json' and state['calls']<=2:
                self.send_response(200);self.end_headers();self.wfile.write(b'{"broken":');return
            request=urllib.request.Request(fx.url+self.path,data=raw if raw else None,method=self.command,
                        headers={key:value for key,value in self.headers.items() if key.lower() not in ('host','content-length')})
            with urllib.request.urlopen(request,timeout=5) as response:body=response.read()
            if mode=='lost-send' and state['calls']==1:
                self.send_response(502);self.end_headers();return
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    base='http://127.0.0.1:'+str(server.server_port)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield base,state
    finally:server.shutdown();server.server_close();thread.join(timeout=5)


class NetworkRecoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixture.AttachmentTest.setUpClass();cls.fx=fixture.AttachmentTest()
    @classmethod
    def tearDownClass(cls):fixture.AttachmentTest.tearDownClass()
    def conf(self,url):
        return types.SimpleNamespace(server=url,token=self.fx.tokens['alice'],project='main',secretary=False,url_source='')

    def test_successful_send_with_lost_response_is_not_duplicated(self):
        key=uuid.uuid4().hex;body={'body':'network fixture','idem_key':key}
        with proxy(self.fx,'lost-send') as (url,state),mock.patch.object(tg.time,'sleep'):
            response=tg.api_retry(self.conf(url),'POST','/api/messages',body)
        self.assertEqual(state['calls'],2)
        with self.fx.s.DB_LOCK:
            rows=self.fx.s.db().execute('SELECT id FROM messages WHERE idem_key=?',(key,)).fetchall()
        self.assertEqual([row[0] for row in rows],[response['id']])

    def test_invalid_json_is_transient_and_recovers(self):
        with proxy(self.fx,'json') as (url,state),mock.patch.object(tg.time,'sleep'):
            response=tg.api_retry(self.conf(url),'GET','/api/me')
        self.assertEqual(response['id'],'alice');self.assertEqual(state['calls'],3)

    def test_last_attempt_relocation_really_retries_and_failure_is_bounded(self):
        conf=self.conf('https://old.example.test')
        with mock.patch.object(tg,'api',side_effect=[tg.NetError('down'),{'ok':True}]) as call,\
             mock.patch.object(tg,'discover_server',return_value=True):
            self.assertEqual(tg.api_retry(conf,'GET','/api/me',tries=1),{'ok':True})
            self.assertEqual(call.call_count,2)
        with mock.patch.object(tg,'api',side_effect=tg.NetError('down')) as call,\
             mock.patch.object(tg,'discover_server',return_value=True):
            with self.assertRaises(SystemExit) as error:tg.api_retry(conf,'GET','/api/me',tries=2)
            self.assertEqual(error.exception.code,3);self.assertEqual(call.call_count,3)

    def test_dead_published_address_is_not_saved_and_probe_has_no_auth(self):
        conf=self.conf(self.fx.url);conf.url_source='fixture';conf.save_server=mock.Mock()
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"version":"fixture","types":[]}'
        with mock.patch.object(tg,'published_candidates',return_value=iter(['http://127.0.0.1:1','http://127.0.0.1:2'])),\
             mock.patch.object(tg.urllib.request,'urlopen',side_effect=[OSError('dead'),Response()]) as open_,\
             mock.patch.object(tg,'_last_discovery',[0]):
            self.assertTrue(tg.discover_server(conf,min_interval=0))
            self.assertEqual(conf.server,'http://127.0.0.1:2');conf.save_server.assert_called_once_with(conf.server)
            self.assertEqual(open_.call_args.args[0],'http://127.0.0.1:2/api/info')

    def test_authenticated_redirect_never_receives_a_token(self):
        with proxy(self.fx,'redirect') as (url,state):
            with self.assertRaises(SystemExit) as error:tg.api(self.conf(url),'GET','/api/me')
            self.assertEqual(error.exception.code,2)
        self.assertEqual(state['target_calls'],0)

    def test_watch_survives_http_refusal_and_network_failure_with_its_cursor(self):
        config=self.fx.work/'recovery-watch.json'
        config.write_text(json.dumps({'server':self.fx.url,'token':self.fx.tokens['alice'],'project':'main',
                                     'url_source':'','auto_update':False,'state_file':str(self.fx.work/'recovery.state.json')}))
        conf=tg.Conf(str(config));conf.save_state({'watch_cursor':7})
        args=types.SimpleNamespace(config=str(config),since=None,all=False,max_burst=50,include_own=False)
        replies=[SystemExit(2),tg.NetError('offline'),{'id':'alice','name':'alice','last_id':100},
                 {'messages':[],'last_id':12},KeyboardInterrupt()]
        with mock.patch.object(tg,'api',side_effect=replies),mock.patch.object(tg,'discover_server',return_value=False),\
             mock.patch.object(tg.time,'sleep'),mock.patch.object(tg,'schedule_update'):
            tg.cmd_watch(conf,args)
        self.assertEqual(conf.load_state()['watch_cursor'],12)


if __name__=='__main__':unittest.main(verbosity=2)
