"""Real browser checks for private navigation, drafts, secret cards and mobile layout."""
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
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<160;i++){if(fn())return;await pause(40)}throw Error('render timeout')};
 try {
  await wait(()=>ME&&MSGS.has(FIXTURE.general)&&document.querySelector('#m'+FIXTURE.general));
  const projects=$('projList').querySelectorAll('.pitem').length;
  $('cBody').value='public draft fixture';
  await selectTab('direct');
  check(!$('directView').classList.contains('hidden'),'private list visible');
  check($('composer').classList.contains('hidden'),'no public composer in conversation list');
  const peer=ME.users.find(u=>u.name==='alice-agent');
  await newDirect(peer.id); const cid=ACTIVE_DM.id;
  check(!$('dmBar').classList.contains('hidden'),'private header visible');
  check($('cBody').value==='','private draft starts empty');
  check($('cTo').classList.contains('hidden'),'recipient fixed in private chat');
  check($('projList').querySelectorAll('.pitem').length===projects,'left sidebar still contains only projects');
  check(!MSGS.has(FIXTURE.general),'general feed excluded from private chat');
  $('cBody').value=('private **fixture** message '+VIEWPORT);$('sendBtn').click();
  await wait(()=>!SENDING&&[...MSGS.values()].some(m=>m.body===('private **fixture** message '+VIEWPORT)));
  const mid=[...MSGS.values()].find(m=>m.body===('private **fixture** message '+VIEWPORT)).id;
  check(document.querySelector('#m'+mid+' strong')?.textContent==='fixture','private Markdown works');
  check(!(await api('GET','/api/messages')).messages.some(m=>m.id===mid),'private message excluded from general API');
  $('cBody').value='private draft fixture';await selectTab('feed');
  check($('cBody').value==='public draft fixture','general draft restored');
  await wait(()=>MSGS.has(FIXTURE.general));
  check(!MSGS.has(mid),'general feed excludes private history');
  await openDirect(cid);check($('cBody').value==='private draft fixture','private draft restored');
  $('secretBtn').click();check(!$('secretDialog').classList.contains('hidden'),'secret editor opened');
  const value='ghp_'+'b'.repeat(36);$('secretValue').value=value;
  $('secretForm').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
  await wait(()=>$('secretDialog').classList.contains('hidden')&&[...MSGS.values()].some(m=>m.secret&&m.id>mid));
  const secret=[...MSGS.values()].find(m=>m.secret&&m.id>mid);
  check(!!document.querySelector('#m'+secret.id+' .secret-card'),'secret card rendered');
  check(!JSON.stringify([...MSGS.values()]).includes(value),'secret value absent from loaded history');
  check(!JSON.stringify([...DRAFTS.values()]).includes(value),'secret value absent from drafts');
  check(!JSON.stringify({...localStorage}).includes(value),'secret value absent from browser storage');
  check($('secretValue').value==='','secret editor cleared after send');
  document.querySelector('#m'+secret.id+' [data-act="secret-revoke"]').click();
  await wait(()=>MSGS.get(secret.id)?.secret?.state==='revoked');
  $('secretBtn').click();$('secretValue').value='discard fixture';
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  check($('secretDialog').classList.contains('hidden')&&$('secretValue').value==='','Escape clears secret');
  await selectTab('feed');await selectTab('direct');
  check($('dmList').querySelector('[data-conversation="'+cid+'"]'),'recent conversation available');
  await openDirect(cid);
  check(document.documentElement.scrollWidth<=innerWidth,'no horizontal page overflow');
  check($('secretBtn').getBoundingClientRect().width>=24,'secret button usable');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
})();
'''


@unittest.skipUnless(Path(FIREFOX).is_file(), 'Headless Firefox is required for browser checks')
class DirectBrowserTest(unittest.TestCase):
    def test_navigation_drafts_secret_cards_and_mobile_layout(self):
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
                    ids={'general':api('POST','/api/messages?project=main',{'body':'general fixture'})['id']}
                    template = (ROOT/'web.html').read_text(encoding='utf-8')
                    for viewport,width,height in [('desktop',1360,1000),('mobile',390,844)]:
                        result_path=work/'result.json';result_path.unlink(missing_ok=True)
                        constants='const FIXTURE='+json.dumps(ids)+'; const VIEWPORT='+json.dumps(viewport)+';'
                        html=template.replace('</body>','<img src="/slow" style="display:none"><script>'+constants+PROBE+'</script></body>')
                        (work/'web.html').write_text(html,encoding='utf-8')
                        profile=work/('profile-'+viewport);profile.mkdir()
                        subprocess.run([FIREFOX,'--headless','--no-remote','--profile',str(profile),'--window-size',f'{width},{height}',
                                       '--screenshot',str(work/(viewport+'.png')),url+'/#token='+token+'&p=main'],env=env,capture_output=True,timeout=45,check=True)
                        artifacts=os.environ.get('TELEGENT_TEST_ARTIFACTS')
                        if artifacts:
                            dest=Path(artifacts);dest.mkdir(parents=True,exist_ok=True)
                            shutil.copyfile(work/(viewport+'.png'),dest/(viewport+'.png'))
                        self.assertTrue(result_path.exists(),'browser did not report completion')
                        result=json.loads(result_path.read_text());self.assertTrue(result.get('ok'),result)
                finally:
                    server.terminate();server.wait(timeout=5)


if __name__=='__main__':unittest.main(verbosity=2)
