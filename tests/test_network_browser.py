"""Real browser recovery: hung requests, incomplete JSON and lost send responses."""
import unittest
import test_direct_browser as browser

PROBE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<300;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
 const original=appFetch,timeout=window.setTimeout;let fault='',lost=false,keys=[];
 appFetch=async(url,options={})=>{
   if(String(url)==='/api/recovery-fixture'){
     if(fault==='json')return new Response('{',{status:200});
     const blocked=()=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true}));
     if(fault==='hang')return blocked();
     if(fault==='body')return {ok:true,json:blocked};
   }
   const response=await original(url,options);
   if(lost&&String(url)==='/api/messages'&&options.method==='POST'){
     const body=JSON.parse(options.body);keys.push(body.idem_key);lost=false;
     if(response.ok)throw new TypeError('simulated lost committed response');
   }
   return response;
 };
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  fault='json';let failure='';try{await api('GET','/api/recovery-fixture');}catch(error){failure=error.message;}
  check(failure===tr('Сервер вернул неполный ответ. Повтори запрос.'),'invalid JSON is a readable request failure, never a null success');
  window.setTimeout=(fn,ms,...args)=>timeout(fn,ms===45000?20:ms,...args);
  for(fault of ['hang','body']){
    let failure='';try{await api('GET','/api/recovery-fixture');}catch(error){failure=error.name;}
    check(failure==='TimeoutError','deadline bounds '+fault+' without pretending the user cancelled');
  }
  fault='hang';const cancel=new AbortController(),request=api('GET','/api/recovery-fixture',null,cancel.signal);cancel.abort();
  let cancelled='';try{await request;}catch(error){cancelled=error.name;}
  check(cancelled==='AbortError','external navigation cancellation stays distinguishable');
  window.setTimeout=timeout;fault='';check((await api('GET','/api/me')).id===ME.id,'successful API operation resumes after all faults');
  resetCompose();$('cBody').value='network send '+VIEWPORT+' '+Date.now();const body=$('cBody').value;
  await addFiles([new File(['network fixture'],'recovery.txt',{type:'text/plain'})]);
  lost=true;$('sendBtn').click();await wait(()=>!SENDING);
  check($('cBody').value===body&&ATTACHED.length===1&&!$('retrySend').classList.contains('hidden'),'lost response preserves text, file and retry action');
  const key=COMPOSE_RETRY.key;check(key===keys[0],'retry retains original send identity');
  $('retrySend').click();await wait(()=>!SENDING&&[...MSGS.values()].some(m=>m.body===body));
  const rows=(await api('GET','/api/messages?limit=1000')).messages.filter(m=>m.body===body);
  check(rows.length===1&&rows[0].attachments.length===1,'retry after committed send has one message and one attachment');
  check(!$('cBody').value&&!ATTACHED.length,'confirmed retry clears the draft only after success');
  localStorage.setItem('tg_language','en');
  await original('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){await original('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
 finally{appFetch=original;window.setTimeout=timeout;}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class NetworkBrowserTest(unittest.TestCase):
    def test_network_recovery_preserves_unsent_content(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
