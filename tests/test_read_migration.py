"""Real browser checks for Markdown, hostile message text and unchanged source."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FIREFOX = shutil.which('firefox') or str(Path(os.environ.get('PROGRAMFILES', 'C:/Program Files'))/'Mozilla Firefox/firefox.exe')
PROBE = r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));
 try {
  for(let i=0;i<150&&!ME?.project;i++)await pause(40);
  if(!ME?.project)throw Error('login failed');
  await pause(800);
  const p=(await api('GET','/api/projects')).projects.find(p=>p.slug==='main');
  const expected=VIEWPORT==='existing'?FIXTURE.first:0;
  if(p.read_upto!==expected)throw Error('wrong migrated position '+JSON.stringify(p));
  if(p.unread!==(VIEWPORT==='existing'?1:2))throw Error('new messages or another account were marked read');
  if(VIEWPORT==='existing'&&localStorage.getItem('tg_seen_main')!==null)throw Error('legacy position retained after success');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack})})}
})();
'''


@unittest.skipUnless(Path(FIREFOX).is_file(), 'Headless Firefox is required for browser checks')
class LegacyReadMigrationTest(unittest.TestCase):
    def test_existing_session_migrates_without_pre_reading_or_cross_account_inheritance(self):
        with tempfile.TemporaryDirectory(prefix='telegent_markdown_') as work_name:
            work = Path(work_name)
            source = (ROOT/'server.py').read_text(encoding='utf-8')
            extra = '''
@route('POST', '/debug/result', auth=False)
def test_result(h,user,q,data):
    with open(os.path.join(HERE,'result.json'),'w') as f: json.dump(data,f)
    return {'ok':True}
@route('GET', '/slow', auth=False)
def test_slow(h,user,q,data):
    time.sleep(9)
    h.send_bytes(200,b'<svg xmlns="http://www.w3.org/2000/svg"/>','image/svg+xml')
'''
            (work/'server.py').write_text(source.replace('if __name__ == "__main__":', extra+'\nif __name__ == "__main__":'), encoding='utf-8')
            env = {k:v for k,v in os.environ.items() if not k.startswith('TELEGENT_')}
            env.update(HOME=str(work), USERPROFILE=str(work), PYTHONIOENCODING='utf-8')
            command = [sys.executable, str(work/'server.py'), '--config', str(work/'server.json'), '--db', str(work/'test.db')]
            subprocess.run(command+['adduser','alice','--kind','human','--owner','alice','--token-out',str(work/'alice.token')], env=env, check=True, capture_output=True)
            token = (work/'alice.token').read_text().strip()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
            url = 'http://127.0.0.1:'+str(port)
            with (work/'server.log').open('w') as log:
                server = subprocess.Popen(command+['serve','--port',str(port)], env=env, stdout=log, stderr=log)
                try:
                    for _ in range(60):
                        try:
                            urllib.request.urlopen(url+'/api/info',timeout=.2).close(); break
                        except OSError:time.sleep(.1)
                    def api(method, path, data=None):
                        req = urllib.request.Request(url+path, method=method,
                              data=json.dumps(data).encode() if data is not None else None,
                              headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                        with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)
                    subprocess.run(command+['adduser','alice-agent','--kind','agent','--owner','alice','--token-out',str(work/'agent.token')],env=env,check=True,capture_output=True)
                    agent_token=(work/'agent.token').read_text().strip()
                    def incoming():
                        req=urllib.request.Request(url+'/api/messages?project=main',method='POST',data=json.dumps({'body':'fixture incoming'}).encode(),headers={'Authorization':'Bearer '+agent_token,'Content-Type':'application/json'})
                        with urllib.request.urlopen(req) as response:return json.load(response)['id']
                    ids={'first':incoming(),'second':incoming()}
                    template = (ROOT/'web.html').read_text(encoding='utf-8')
                    for viewport,width,height in [('fresh',390,844),('existing',1360,1000)]:
                        result_path=work/'result.json';result_path.unlink(missing_ok=True)
                        constants='const FIXTURE='+json.dumps(ids)+'; const VIEWPORT='+json.dumps(viewport)+';'
                        setup="Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'hidden'});localStorage.setItem('tg_seen_main',"+json.dumps(str(ids['first']))+");"
                        if viewport=='existing':setup+="localStorage.setItem('tg_token',"+json.dumps(token)+");"
                        html=template.replace('<script>','<script>'+setup,1).replace('</body>','<img src="/slow" style="display:none"><script>'+constants+PROBE+'</script></body>')
                        (work/'web.html').write_text(html,encoding='utf-8')
                        profile=work/('profile-'+viewport);profile.mkdir()
                        subprocess.run([FIREFOX,'--headless','--no-remote','--profile',str(profile),'--window-size',f'{width},{height}',
                                       '--screenshot',str(work/(viewport+'.png')),url+'/#token='+token+'&p=main'],env=env,capture_output=True,timeout=45,check=True)
                        self.assertTrue(result_path.exists(),'browser did not report completion')
                        result=json.loads(result_path.read_text());self.assertTrue(result.get('ok'),result)
                finally:
                    server.terminate();server.wait(timeout=5)


if __name__=='__main__':unittest.main(verbosity=2)
