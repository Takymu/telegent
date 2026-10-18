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
window.addEventListener('error', e => fetch('/debug/result', {method:'POST', body:JSON.stringify({error:e.message})}));
(async () => {
 const delay=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<120;i++){if(fn())return;await delay(40)}throw Error('render timeout')};
 try {
  await wait(()=>ME&&MSGS.has(FIXTURE.main)&&document.querySelector(`#m${FIXTURE.main} .md`));
  const node=document.querySelector(`#m${FIXTURE.main} .md`);
  check(node.querySelector('h1')?.textContent==='Heading','heading');
  check(node.querySelector('strong')?.textContent==='Bold:','bold list label');
  check(node.querySelector('em')?.textContent==='italic','emphasis');
  check(node.querySelector('del')?.textContent==='old','strikethrough');
  check(node.querySelector('ul > li ul > li')?.textContent==='nested','nested list');
  check(node.querySelector('ol')?.start===3&&node.querySelectorAll('ol > li').length===2,'ordered list');
  check(node.querySelector('blockquote')?.textContent==='quote','quote');
  check(node.querySelector('code')?.textContent==='x < y && y > 0','inline code');
  check(node.querySelector('pre code')?.textContent==='print("<b>**literal**</b>")','fenced code preserved');
  check(node.querySelectorAll('a').length===2,'explicit and plain links');
  check([...node.querySelectorAll('a')].every(a=>a.protocol==='https:'&&a.target==='_blank'&&a.rel.includes('noopener')&&a.rel.includes('noreferrer')),'safe link attributes');
  const attack=document.querySelector(`#m${FIXTURE.attack} .md`);
  check(!attack.querySelector('script,img,svg,iframe,object,style,form,button,input'),'no raw HTML or remote images');
  check(!attack.querySelector('[onclick],[onerror],[onload],[data-act]'),'no injected handlers or actions');
  check([...attack.querySelectorAll('a')].every(a=>['http:','https:'].includes(a.protocol)),'unsafe URL schemes blocked');
  check(attack.textContent.includes('<img')&&attack.textContent.includes('javascript:'),'hostile source visible as text');
  await delay(50);check(!window.__markdown_xss,'no payload executed');
  const plain=document.querySelector(`#m${FIXTURE.plain} .md`);
  check(plain.textContent.includes('file_name_with_underscores'),'underscores in names preserved');
  check(plain.querySelectorAll('br').length===1,'plain line break');
  const started=performance.now();
  markdown('['.repeat(100000)+'](javascript:alert(1))');markdown('*'.repeat(200000));
  check(performance.now()-started<2000,'bounded hostile-input rendering');
  let copied=null;
  Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{copied=text}}});
  document.querySelector(`#m${FIXTURE.main} [data-act="more"]`).click();
  $('menu').querySelector('[data-act="copytext"]').click();await wait(()=>copied!==null);
  check(copied===MSGS.get(FIXTURE.main).body,'copy returns exact source');
  document.querySelector('[data-tab="results"]').click();
  await wait(()=>RES.some(m=>m.id===FIXTURE.result));
  RES_OPEN.add(FIXTURE.result);renderResults();
  check($('resultsBox').querySelector('.rdetail strong')?.textContent==='Result','result body Markdown');
  const a=$('resultsBox').querySelector('.rdetail a');let prevented=true;
  const handler=e=>{if(e.target===a){prevented=e.defaultPrevented;e.preventDefault()}};
  document.addEventListener('click',handler);a.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));document.removeEventListener('click',handler);
  check(!prevented,'Markdown link keeps native click handling');
  document.querySelector('[data-tab="feed"]').click();
  const longCode=document.querySelector(`#m${FIXTURE.code} pre`);
  check(longCode&&longCode.scrollWidth>longCode.clientWidth,'long code scrolls inside its block');
  check(document.documentElement.scrollWidth<=innerWidth,'no horizontal page overflow');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
})();
'''


@unittest.skipUnless(Path(FIREFOX).is_file(), 'Headless Firefox is required for browser checks')
class MarkdownTest(unittest.TestCase):
    def test_rendering_security_and_source_preservation(self):
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
                    body = '# Heading\n- **Bold:** text\n- *italic* and ~~old~~\n  - nested\n- `x < y && y > 0`\n\n3. third\n4. fourth\n\n> quote\n\n[docs](https://example.invalid/docs?a=1&b=2)\nhttps://example.invalid/plain\n\n```python\nprint("<b>**literal**</b>")\n```'
                    attack = '<img src=x onerror="window.__markdown_xss=1"><script>window.__markdown_xss=1</script>\n<svg onload="window.__markdown_xss=1"></svg>\n[bad](javascript:alert(1)) [data](data:text/html,hello) ![image](https://example.invalid/image)\n[<img src=x>](https://example.invalid/\"quoted\")'
                    bodies = {'main':body, 'attack':attack, 'plain':'file_name_with_underscores\nnext line',
                              'code':'```\n'+'long_code_'*120+'\n```','result':'**Result** [docs](https://example.invalid/result)'}
                    ids = {}
                    for key,text in bodies.items():
                        data = {'body':text}
                        if key=='result':data.update(type='result', result={'metric':'acc','dataset':'val','value':.9})
                        ids[key]=api('POST','/api/messages?project=main',data)['id']
                    template = (ROOT/'web.html').read_text(encoding='utf-8')
                    for viewport,width,height in [('desktop',1360,1000),('mobile',390,844)]:
                        result_path=work/'result.json';result_path.unlink(missing_ok=True)
                        constants='const FIXTURE='+json.dumps(ids)+'; const VIEWPORT='+json.dumps(viewport)+';'
                        html=template.replace('</body>','<img src="/slow" style="display:none"><script>'+constants+PROBE+'</script></body>')
                        (work/'web.html').write_text(html,encoding='utf-8')
                        profile=work/('profile-'+viewport);profile.mkdir()
                        subprocess.run([FIREFOX,'--headless','--no-remote','--profile',str(profile),'--window-size',f'{width},{height}',
                                       '--screenshot',str(work/(viewport+'.png')),url+'/#token='+token+'&p=main'],env=env,capture_output=True,timeout=45,check=True)
                        self.assertTrue(result_path.exists(),'browser did not report completion')
                        result=json.loads(result_path.read_text());self.assertTrue(result.get('ok'),result)
                    for key,mid in ids.items():
                        self.assertEqual(api('GET',f'/api/messages/{mid}?project=main')['message']['body'],bodies[key])
                finally:
                    server.terminate();server.wait(timeout=5)


if __name__=='__main__':unittest.main(verbosity=2)
