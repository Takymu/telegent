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
  const edit=async id=>{
    document.querySelector('#m'+id+' [data-act="more"]').click();
    document.querySelector('#menu [data-act="edit"]').click();
    await wait(()=>$('editDialog').open);
  };
  await edit(FIXTURE.general);$('editBody').value='updated **general** fixture '+VIEWPORT;$('editSave').click();
  await wait(()=>!$('editDialog').open&&MSGS.get(FIXTURE.general).body==='updated **general** fixture '+VIEWPORT);
  check(!!document.querySelector('#m'+FIXTURE.general+' .medited'),'edited message visibly marked');
  check($('cBody').value==='public draft fixture','editing leaves the composer draft alone');
  await edit(FIXTURE.general);
  const latest=MSGS.get(FIXTURE.general);
  await api('POST','/api/messages/'+FIXTURE.general+'/edit',{body:'other device fixture '+VIEWPORT,revision:latest.revision});
  await wait(()=>MSGS.get(FIXTURE.general).body==='other device fixture '+VIEWPORT);
  $('editBody').value='unsaved conflict fixture';$('editSave').click();
  await wait(()=>!$('editSave').disabled&&$('editErr').textContent);
  check($('editDialog').open&&$('editBody').value==='unsaved conflict fixture','conflict keeps unsaved text');$('editCancel').click();
  document.querySelector('#m'+FIXTURE.agent+' [data-act="more"]').click();
  check(!document.querySelector('#menu [data-act="edit"],#menu [data-act="delete"]'),'agent messages have no human editing action');closeMenu();
  const disposable=(await api('POST','/api/messages',{body:'delete browser fixture'})).id;await refresh();
  const realConfirm=window.confirm;window.confirm=()=>false;
  document.querySelector('#m'+disposable+' [data-act="more"]').click();document.querySelector('#menu [data-act="delete"]').click();
  await pause(50);check(!MSGS.get(disposable).deleted,'delete can be cancelled');
  window.confirm=()=>true;
  document.querySelector('#m'+disposable+' [data-act="more"]').click();document.querySelector('#menu [data-act="delete"]').click();
  await wait(()=>MSGS.get(disposable)?.deleted&&document.querySelector('#m'+disposable+'.deleted'));
  window.confirm=realConfirm;
  check(!document.querySelector('#m'+disposable+' [data-act="edit"]'),'deleted message cannot be edited');
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
  await edit(mid);$('editBody').value='private **fixture** edited '+VIEWPORT;$('editSave').click();
  await wait(()=>!$('editDialog').open&&MSGS.get(mid).edited);
  check(MSGS.get(mid).conversation_id===cid,'editing preserves private conversation');
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
  $('dmBack').click();await selectTab('feed');
  check($('cBody').value==='public draft fixture','leaving private list cannot overwrite public draft');
  await addFiles([new File(['fixture'],'draft.txt',{type:'text/plain'})]);
  startReply(MSGS.get(FIXTURE.general));$('cancelReply').click();
  check($('cBody').value==='public draft fixture'&&ATTACHED.length===1,'cancel quote preserves text and file');
  await openDirect(cid);$('dmBack').click();
  await newDirect(ME.users.find(u=>u.name==='bob-agent').id);
  check($('cBody').value==='','another conversation starts with its own draft');
  $('cBody').value='other private draft';$('dmBack').click();await selectTab('feed');
  check($('cBody').value==='public draft fixture'&&ATTACHED[0]?.file.name==='draft.txt','list and another DM preserve public draft and file');
  await setProject(FIXTURE.second);
  check($('cBody').value==='','another project starts empty');$('cBody').value='second project draft';
  await setProject('main');
  check($('cBody').value==='public draft fixture'&&ATTACHED.length===1,'cross-project draft and file restored');
  $('cPending').querySelector('[data-remove-file]').click();
  $('cBody').value='sent draft '+VIEWPORT;$('sendBtn').click();
  await wait(()=>!SENDING&&[...MSGS.values()].some(m=>m.body==='sent draft '+VIEWPORT));
  await selectTab('direct');await selectTab('feed');
  check($('cBody').value===''&&!st.get(draftStorageKey()),'sent text never returns as a draft');
  const nav=$('mobileNav');
  check(!!$('attachBtn').querySelector('svg'),'paperclip is a platform-independent vector');
  $('devChatHint').classList.remove('hidden');
  check((getComputedStyle($('devChatHint')).display==='none')===(VIEWPORT==='mobile'),'description hidden only on mobile');
  $('devChatHint').classList.add('hidden');
  if(VIEWPORT==='mobile') {
    check(nav.getBoundingClientRect().bottom<=innerHeight&&nav.getBoundingClientRect().height>48,'navigation reachable at bottom');
    check([...nav.querySelectorAll('button')].every(b=>b.getBoundingClientRect().height>=48),'navigation touch targets');
    check(['attachBtn','sendBtn','cOptsBtn'].every(id=>$(id).getBoundingClientRect().width>=48),'composer touch targets');
    const touch=(target,type,points)=>{const e=new Event(type,{bubbles:true,cancelable:true});
      Object.defineProperties(e,{touches:{value:type==='touchend'?[]:points},changedTouches:{value:points}});target.dispatchEvent(e)};
    const swipe=async(target,dx,dy=0,x=dx>0?100:270,multi=false)=>{
      const points=(xx,yy)=>[{clientX:xx,clientY:yy},...(multi?[{clientX:xx+10,clientY:yy}]:[])];
      touch(target,'touchstart',points(x,240));touch(target,'touchmove',points(x+dx,240+dy));touch(target,'touchend',points(x+dx,240+dy));await pause(120);
    };
    const sc=$('scroller');
    await swipe(sc,120);check($('projects').classList.contains('open'),'right swipe opens projects');
    check($('projects').getBoundingClientRect().bottom<=nav.getBoundingClientRect().top+1,'project panel leaves navigation accessible');
    await swipe($('projects'),-120);check(!$('projects').classList.contains('open'),'left swipe closes projects');
    await swipe(sc,-120);check($('aside').classList.contains('open'),'left swipe opens participants before DMs');
    await swipe($('aside'),-120);await wait(()=>TAB==='direct');
    check(!ACTIVE_DM&&!$('aside').classList.contains('open'),'next left swipe opens private list without a keyboard');
    await openDirect(cid);await swipe(sc,120);check(!ACTIVE_DM&&TAB==='direct','right swipe leaves private conversation');
    await swipe(sc,120);check($('aside').classList.contains('open'),'right swipe returns from DMs to participants');
    await swipe($('aside'),120);await wait(()=>TAB==='feed'&&!$('aside').classList.contains('open'));
    check(nav.querySelector('[aria-current="page"]').dataset.mobile==='feed','reverse swipe returns from participants to general chat');
    await swipe(sc,20,150);check(TAB==='feed'&&!$('projects').classList.contains('open'),'vertical scrolling does not navigate');
    await swipe(sc,120,0,5);check(!$('projects').classList.contains('open'),'system edge gesture retained');
    await swipe(sc,-120,0,270,true);check(TAB==='feed','multitouch never navigates');
    await swipe($('cBody'),-120);check(TAB==='feed','editing gesture does not navigate');
    const pre=document.createElement('pre');pre.textContent='horizontal fixture';sc.append(pre);
    await swipe(pre,-120);check(TAB==='feed','code scrolling does not navigate');pre.remove();
    const nested=document.createElement('div');nested.style.cssText='width:50px;overflow-x:auto';nested.innerHTML='<div style="width:500px">nested scroll</div>';sc.append(nested);
    await swipe(nested.firstChild,-120);check(TAB==='feed','nested horizontal scrolling does not navigate');nested.remove();
    const selection=getSelection(),range=document.createRange();range.selectNodeContents(document.querySelector('#m'+FIXTURE.general+' .mtext'));selection.addRange(range);
    await swipe(sc,-120);check(TAB==='feed','selected text does not trigger navigation');selection.removeAllRanges();
    nav.querySelector('[data-mobile="more"]').click();
    await wait(()=>TAB==='more'&&!$('moreView').classList.contains('hidden'));
    check($('menu').classList.contains('hidden'),'More is a full section, not a popup');
    document.querySelector('#moreView [data-tab="results"]').click();await wait(()=>TAB==='results');
    nav.querySelector('[data-mobile="feed"]').click();await wait(()=>TAB==='feed');
    check(nav.querySelector('[data-mobile="feed"]').getAttribute('aria-current')==='page','bottom navigation reflects selected tab');
  } else check(getComputedStyle(nav).display==='none','desktop keeps its existing navigation');
  $('cBody').value='reload public '+VIEWPORT;$('cBody').dispatchEvent(new Event('input'));
  await pause(300);check(JSON.parse(st.get(draftStorageKey())).body===$('cBody').value,'typing persists draft before navigation');
  await openDirect(cid);$('cBody').value='reload private '+VIEWPORT;
  await addFiles([new File(['fixture'],'private-draft.txt')]);$('dmBack').click();await selectTab('feed');
  check($('cBody').value==='reload public '+VIEWPORT,'public draft retained before browser restart');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
})();
'''

RESTORE_PROBE = r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 try {
  for(let i=0;i<160&&(!ME?.project||COMPOSE_SCOPE!==draftKey());i++)await pause(40);
  check($('cBody').value==='reload public '+VIEWPORT,'public draft survives browser restart');
  await loadConversations();const cid=CONVERSATIONS.find(c=>c.peer==='alice-agent').id;
  await openDirect(cid);check($('cBody').value==='reload private '+VIEWPORT,'private draft survives browser restart');
  check(!ATTACHED.length,'browser file objects are not serialized');
  check($('sendErr').textContent.includes('private-draft.txt'),'restored draft explains that files must be selected again');
  await setProject(FIXTURE.second);check($('cBody').value==='second project draft','other project draft survives browser restart');
  const real=ME;ME={...real,id:'other-account'};restoreDraft();
  check($('cBody').value==='','another account cannot inherit a local draft');ME=real;restoreDraft();
  st.set('tg_draft_v1:other-account:main:general','other account fixture');forgetDrafts();saveDraft();
  check(!Object.keys(localStorage).some(k=>k.startsWith(draftStoragePrefix())),'logout clears own persisted drafts');
  check(st.get('tg_draft_v1:other-account:main:general')==='other account fixture','logout leaves other accounts alone');
  st.set('tg_draft_v1:other-account:main:general',null);
  showNoProject();
  check([...$('mobileNav').querySelectorAll('button')].every(b=>b.disabled===(b.dataset.mobile!=='projects')),'no-project navigation only offers projects');
  check($('composer').classList.contains('hidden')&&$('directView').classList.contains('hidden'),'no-project state hides previous editor and private list');
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
                    def api(method, path, data=None, caller_token=None):
                        req = urllib.request.Request(url+path, method=method,
                              data=json.dumps(data).encode() if data is not None else None,
                              headers={'Authorization':'Bearer '+(caller_token or token),'Content-Type':'application/json'})
                        with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)
                    subprocess.run(command+['adduser','alice-agent','--kind','agent','--owner','alice','--token-out',str(work/'agent.token')],env=env,check=True,capture_output=True)
                    subprocess.run(command+['adduser','bob-agent','--kind','agent','--owner','alice','--token-out',str(work/'other-agent.token')],env=env,check=True,capture_output=True)
                    ids={'general':api('POST','/api/messages?project=main',{'body':'general fixture'})['id']}
                    ids['agent']=api('POST','/api/messages?project=main',{'body':'agent fixture'},(work/'agent.token').read_text().strip())['id']
                    ids['second']=api('POST','/api/projects',{'title':'Second fixture','slug':'second'})['project']['slug']
                    template = (ROOT/'web.html').read_text(encoding='utf-8')
                    for viewport,width,height in [('desktop',1360,1000),('mobile',390,844)]:
                        constants='const FIXTURE='+json.dumps(ids)+'; const VIEWPORT='+json.dumps(viewport)+';'
                        profile=work/('profile-'+viewport);profile.mkdir()
                        for stage,probe in [('navigation',PROBE),('restored',RESTORE_PROBE)]:
                            result_path=work/'result.json';result_path.unlink(missing_ok=True)
                            html=template.replace('</body>','<img src="/slow" style="display:none"><script>'+constants+probe+'</script></body>')
                            (work/'web.html').write_text(html,encoding='utf-8')
                            name=viewport+'-'+stage+'.png'
                            subprocess.run([FIREFOX,'--headless','--no-remote','--profile',str(profile),'--window-size',f'{width},{height}',
                                           '--screenshot',str(work/name),url+'/#token='+token+'&p=main'],env=env,capture_output=True,timeout=45,check=True)
                            artifacts=os.environ.get('TELEGENT_TEST_ARTIFACTS')
                            if artifacts:
                                dest=Path(artifacts);dest.mkdir(parents=True,exist_ok=True)
                                shutil.copyfile(work/name,dest/name)
                            self.assertTrue(result_path.exists(),'browser did not report completion')
                            result=json.loads(result_path.read_text());self.assertTrue(result.get('ok'),result)
                finally:
                    server.terminate();server.wait(timeout=5)


if __name__=='__main__':unittest.main(verbosity=2)
