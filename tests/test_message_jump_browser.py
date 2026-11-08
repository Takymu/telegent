"""Message links land on their target despite pagination and concurrent refreshes."""
import unittest
import test_direct_browser as browser

PROBE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<300;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
 const visible=id=>{const r=$('m'+id)?.getBoundingClientRect(),s=SC.getBoundingClientRect();return !!r&&r.bottom>s.top+20&&r.top<s.bottom-20};
 const real=api;
 const actor=async data=>{
  const r=await fetch('/api/messages',{method:'POST',headers:{Authorization:'Bearer '+FIXTURE.agent_token,'Content-Type':'application/json','X-Telegent-Project':'main'},body:JSON.stringify(data)});
  const d=await r.json();if(!r.ok)throw Error(d.error);return d.id;
 };
 const open=id=>{const button=document.querySelector('#workBox [data-act="jump"][data-id="'+id+'"]');check(!!button,'waiting row exists');button.click();};
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.size);
  api=(method,path,...args)=>path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args);
  await real('POST','/api/waiting/dismiss',{all:true});
  const old=await actor({body:'Old question '+VIEWPORT,type:'question',to_ids:[ME.id]});
  for(let i=0;i<110;i++)await actor({body:'Thread fixture '+i+'\n'+('Visible fixture context. '.repeat(6)),reply_to:old});
  const recent=await actor({body:'Recent question '+VIEWPORT,type:'question',to_ids:[ME.id]});
  for(let i=0;i<15;i++)await actor({body:'Recent context '+i+'\n'+('Visible fixture context. '.repeat(6))});
  const localParent=await actor({body:'Already rendered parent '+VIEWPORT+'\n'+('Local jump context. '.repeat(5))});
  const localReply=await actor({body:'Reply with local quote '+VIEWPORT,reply_to:localParent});
  await selectTab('feed');await reload();
  check(!!$('m'+localParent)&&!!$('m'+localReply),'local reply pair is rendered');
  let localLoads=0,emptyFrames=0;
  const observer=new MutationObserver(()=>{if(!document.querySelector('#feed [data-msg]'))emptyFrames++});observer.observe($('feed'),{childList:true,subtree:true});
  api=async(method,path,...args)=>{if(path.startsWith('/api/messages'))localLoads++;return path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args)};
  $('m'+localReply).querySelector('.mquote').click();
  await wait(()=>$('m'+localParent)?.classList.contains('highlight'));await pause(100);observer.disconnect();
  check(visible(localParent),'local quote scrolls to its rendered parent');
  check(localLoads===0,'local quote jump performs no message API request');
  check(emptyFrames===0,'local quote jump never empties the feed');
  api=(method,path,...args)=>path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args);
  $('cBody').value='Public jump draft';$('cBody').dispatchEvent(new Event('input'));
  $('q').value='Pending search excludes the target';$('q').dispatchEvent(new Event('input'));
  await selectTab('work');await loadWork();
  open(old);await wait(()=>TAB==='feed'&&$('m'+old)?.classList.contains('highlight'));await pause(500);
  check(visible(old),'old root remains visible when its thread exceeds a history page');
  check(!THREAD&&!hasFilters(),'opening a message keeps the surrounding general chat');
  check(MSGS.has(old+1)&&!ACTIVE_DM,'nearby thread replies remain in the general chat');
  check($('cBody').value==='Public jump draft','jump preserves the general chat draft');
  const cursor=(await real('GET','/api/me')).project.read_upto;
  SC.scrollTop=SC.scrollHeight;await pause(350);await flushReads();
  check((await real('GET','/api/me')).project.read_upto===cursor,'bottom of a target window is not the end of chat for read receipts');
  await returnView();check(TAB==='work','return restores the waiting list');await loadWork();
  open(recent);await wait(()=>TAB==='feed'&&$('m'+recent)?.classList.contains('highlight'));
  await refresh();await pause(400);check(visible(recent),'receipt refresh cannot interrupt the jump');
  const cid=(await real('POST','/api/conversations',{peer_id:'alice-agent'})).conversation.id;
  const privateId=await actor({body:'Private question '+VIEWPORT,type:'question',to_ids:[ME.id],conversation_id:cid});
  for(let i=0;i<12;i++)await actor({body:'Private context '+i+'\n'+('Visible fixture. '.repeat(7)),conversation_id:cid});
  await loadConversations();await openDirect(cid);
  $('cBody').value='Private jump draft';$('cBody').dispatchEvent(new Event('input'));
  await selectTab('work');await loadWork();
  let release;const held=[];
  api=async(method,path,...args)=>{
   if(path.startsWith('/api/wait?'))return new Promise(()=>{});
   if(path.startsWith('/api/messages?')&&!path.includes('waiting_me=1')&&!path.includes('pending_approval=')){
    const data=await real(method,path,...args);await new Promise(r=>held.push(r));return data;
   }
   return real(method,path,...args);
  };
  open(privateId);await wait(()=>held.length>=1);check(!document.querySelector('#feed [data-msg]'),'jump does not briefly display cached tail messages');
  api=(method,path,...args)=>path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args);
  for(const resolve of held)resolve();
  await wait(()=>ACTIVE_DM?.id===cid&&$('m'+privateId)?.classList.contains('highlight'));await refresh();await pause(300);
  check(visible(privateId)&&!MSGS.has(recent),'private target is visible without public messages');
  check($('cBody').value==='Private jump draft','private draft belongs to the selected conversation');
  await selectTab('work');await loadWork();
  api=async(method,path,...args)=>{
   if(path.startsWith('/api/messages?')&&(path.includes('before=')||path.includes('after=')))throw Error('Jump load interrupted');
   return path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args);
  };
  open(recent);await wait(()=>document.body.textContent.includes('Jump load interrupted'));
  check(!$('m'+recent),'failed jump explains the error instead of showing another message');
  api=(method,path,...args)=>path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args);
  await returnView();await loadWork();
  const delayed=[];
  api=async(method,path,...args)=>{
   if(path.startsWith('/api/messages?')&&(path.includes('before=')||path.includes('after='))){
    const data=await real(method,path,...args);await new Promise(r=>delayed.push(r));return data;
   }
   return path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args);
  };
  open(recent);await wait(()=>delayed.length===2);await selectTab('board');
  for(const resolve of delayed)resolve();await pause(150);
  check(TAB==='board'&&!MSGS.has(recent),'late target page cannot replace a newer navigation in the same project');
  api=(method,path,...args)=>path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args);
  await selectTab('work');await loadWork();
  api=async(method,path,...args)=>{
   if(path==='/api/messages/'+recent){const data=await real(method,path,...args);await new Promise(r=>release=r);return data;}
   return path.startsWith('/api/wait?')?new Promise(()=>{}):real(method,path,...args);
  };
  open(recent);await wait(()=>release);await setProject(FIXTURE.second);release();await pause(250);
  check(PROJECT===FIXTURE.second&&!MSGS.has(recent),'late message lookup cannot undo a later project switch');
  await setProject('main');await selectTab('work');await loadWork();
  localStorage.setItem('tg_language','en');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,checks,viewport:VIEWPORT})});
 }catch(error){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:error.message,stack:error.stack,checks,viewport:VIEWPORT})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class MessageJumpBrowserTest(unittest.TestCase):
    def test_waiting_message_links_on_desktop_and_mobile(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try: browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally: browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
