"""Attention badges and their list share snapshots despite delayed network responses."""
import unittest
import test_direct_browser as browser

PROBE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<300;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
 const actor=async data=>{
   const r=await fetch('/api/messages',{method:'POST',headers:{Authorization:'Bearer '+FIXTURE.agent_token,'Content-Type':'application/json','X-Telegent-Project':'main'},body:JSON.stringify(data)});
   const body=await r.json();if(!r.ok)throw Error(body.error);return body.id;
 };
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  const real=api;
  // Pause background long-poll for deterministic ordering; keep all tested requests real.
  api=(method,path,...args)=>path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args);
  await real('POST','/api/waiting/dismiss',{all:true});
  const mid=await actor({body:'Attention question '+VIEWPORT,type:'question',to_ids:[ME.id]});
  await refreshSide();check(!$('waitChip').classList.contains('hidden'),'badge shows unanswered question');
  $('waitChip').click();await wait(()=>$('workBox').querySelector('[data-reminder="'+mid+'"]'));
  check(TAB==='work'&&WORK_MODE==='waiting','badge opens the matching list');
  await real('POST','/api/messages',{body:'Answered fixture',reply_to:mid});await loadWork();
  check(!$('workBox').querySelector('[data-reminder]')&&$('waitChip').classList.contains('hidden'),'reply clears list and badge together');
  const pending=await actor({body:'Delayed attention fixture',type:'question',to_ids:[ME.id]});
  await loadWork();check(!!$('workBox').querySelector('[data-reminder="'+pending+'"]'),'new request appears');
  let release,held=false;
  api=async(method,path,...args)=>{
   if(!held&&path.includes('waiting_me=1')){
    held=true;const data=await real(method,path,...args);await new Promise(r=>release=r);return data;
   }
   return path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args);
  };
  const old=loadAttention('waiting');await wait(()=>release);
  await real('POST','/api/waiting/dismiss',{ids:[pending]});await loadWork();release();await old;
  check(!$('workBox').querySelector('[data-reminder]')&&$('waitChip').classList.contains('hidden'),'late positive count cannot replace the newer empty list');
  await actor({body:'Project scoped attention',type:'question',to_ids:[ME.id]});await loadWork();
  check(!$('waitChip').classList.contains('hidden'),'main project has a reminder');
  held=false;release=null;const foreign=loadAttention('waiting');await wait(()=>release);
  await setProject(FIXTURE.second);release();await foreign;await selectTab('work');await loadWork();
  check($('waitChip').classList.contains('hidden')&&!$('workBox').querySelector('[data-reminder]'),'project switch clears old counts and ignores late rows');
  await setProject('main');await selectTab('work');await loadWork();
  check(!$('waitChip').classList.contains('hidden'),'returning restores only this project reminders');
  api=async(method,path,...args)=>{if(path.includes('waiting_me=1'))throw Error('Attention network fixture');return path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args);};
  await loadWork();check($('waitChip').classList.contains('hidden')&&$('workBox').textContent.includes('Attention network fixture'),'failure explains unavailable list instead of retaining a stale count');
  api=(method,path,...args)=>path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args);
  await loadWork();check(!$('waitChip').classList.contains('hidden')&&!!$('workBox').querySelector('[data-reminder]'),'retry restores count and rows together');
  await real('POST','/api/waiting/dismiss',{all:true});await loadWork();
  await selectTab('feed');localStorage.setItem('tg_language','en');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,checks,viewport:VIEWPORT})});
 }catch(error){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:error.message,stack:error.stack,checks,viewport:VIEWPORT})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class AttentionBrowserTest(unittest.TestCase):
    def test_attention_count_list_and_project_ordering(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
