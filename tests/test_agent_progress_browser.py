"""RU/EN desktop/mobile agent progress, live updates, escaped text and stale responses."""
import unittest
import test_direct_browser as browser

PROBE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<350;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  const name='progress-'+VIEWPORT+'-'+Date.now();
  const invitation=await api('POST','/api/invites',{name,kind:'agent'});
  const joined=await (await fetch('/api/join',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:invitation.code})})).json();
  const agent=async payload=>{const response=await fetch('/api/progress',{method:'POST',headers:{Authorization:'Bearer '+joined.token,'Content-Type':'application/json','X-Telegent-Project':'main'},body:JSON.stringify(payload)});if(!response.ok)throw Error('publish '+response.status);return response.json()};
  await loadPresence();
  await showMember(joined.id);check(!!$('infoBody').querySelector('[data-ux="progress"]'),'agent card opens progress tab');
  $('infoBody').querySelector('[data-ux="progress"]').click();await wait(()=>$('progressStatus')?.textContent.includes('100'));
  check(!$('progressEvents').querySelector('article'),'empty progress explains missing updates');
  const mid=(await api('POST','/api/messages',{body:'Progress fixture task'})).id;
  const text='Checking <img src=x onerror="window.__progress_xss=1"> fixture';
  await agent({message_id:mid,key:'first-'+mid,body:text,state:'working'});
  await wait(()=>$('progressEvents')?.textContent.includes('Checking'));
  check($('progressEvents').textContent.includes(text)&&!$('progressEvents').querySelector('img,script')&&!window.__progress_xss,'progress text stays escaped');
  await agent({message_id:mid,key:'next-'+mid,body:'Fixture checks passed',state:'completed'});
  await wait(()=>$('progressEvents')?.textContent.includes('Fixture checks passed'));
  check($('progressEvents').querySelectorAll('article').length===2,'new steps arrive without reopening');
  check($('infoDialog').getBoundingClientRect().width<=innerWidth,'mobile progress fits viewport');
  const normal=api;let release;
  api=async(method,path,...rest)=>path.startsWith('/api/progress')?new Promise(r=>release=r):normal(method,path,...rest);
  const pending=showProgress(joined.id);await wait(()=>!!release);
  showInfo('Other fixture','<p>Other view</p>');release({events:[{id:999,message_id:mid,body:'Must not appear',state:'working',time:new Date().toISOString(),subject:'fixture'}]});await pending;
  check($('infoBody').textContent==='Other view','late progress cannot replace another view');api=normal;
  await showProgress(joined.id);await setProject(FIXTURE.second);await pause(50);
  check(!$('infoDialog').open&&!$('progressEvents'),'project change clears progress');
  await setProject('main');await showProgress(joined.id);
  localStorage.setItem('tg_language','en');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,checks,viewport:VIEWPORT})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,checks,viewport:VIEWPORT})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class ProgressBrowserTest(unittest.TestCase):
    def test_progress_tabs_live_updates_and_scope(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
