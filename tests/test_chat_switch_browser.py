"""Real desktop/mobile chat switches while foreground network responses are held back."""
import unittest
import test_direct_browser as browser

PROBE = r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<400;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
 const original=appFetch;let hold=false,pending=[];
 appFetch=(url,options={})=>{
   const path=new URL(url,location.href);
   if(hold&&(!options.method||options.method==='GET')&&(path.pathname==='/api/me'||path.pathname==='/api/messages'))
     return new Promise((resolve,reject)=>pending.push(()=>original(url,options).then(resolve,reject)));
   return original(url,options);
 };
 const release=()=>{const requests=pending;pending=[];for(const start of requests)start();};
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  const main=PROJECT,other=FIXTURE.second;
  const publicId=(await api('POST','/api/messages',{body:'cached public '+VIEWPORT})).id;
  const secondId=(await api('POST','/api/messages?project='+other,{body:'prefetched second '+VIEWPORT})).id;
  const cid=(await api('POST','/api/conversations',{peer_id:'alice-agent'})).conversation.id;
  const privateId=(await api('POST','/api/messages',{body:'cached private '+VIEWPORT,conversation_id:cid})).id;
  await loadProjects();await reload();await wait(()=>!CHAT_WARMING);
  for(const meta of CHAT_META.values())meta.warmedAt=0;
  const cursor=(await api('GET','/api/projects')).projects.find(p=>p.slug===other).read_upto;
  await warmChats();
  check((await api('GET','/api/projects')).projects.find(p=>p.slug===other).read_upto===cursor,'prefetch does not mark an unopened project read');
  check([...CHAT_PAGES.values()].some(row=>row.data.messages.some(m=>m.id===secondId)),'another project is fetched before its first click');
  check([...CHAT_PAGES.values()].some(row=>row.data.messages.some(m=>m.id===privateId)),'private history is prefetched separately');
  $('cBody').value='draft '+VIEWPORT;
  hold=true;
  let projectFinished=false;const switched=setProject(other).then(()=>projectFinished=true);
  await pause(0);
  check(PROJECT===other&&ME.project.slug===other&&$('projTitle').textContent===ME.project.title,'project header changes without a network response');
  check(MSGS.has(secondId)&&document.querySelector('#m'+secondId),'prefetched project messages paint while all message/me requests are held');
  check(!MSGS.has(publicId)&&!MSGS.has(privateId),'cached project never contains another project or private messages');
  check(projectFinished,'cached project navigation completes without waiting for its refresh');
  hold=false;release();await switched;await pause(200);
  await setProject(main);await wait(()=>MSGS.has(publicId));
  check($('cBody').value==='draft '+VIEWPORT,'project draft survives cached switch');
  hold=true;
  let directFinished=false;const direct=openDirect(cid).then(()=>directFinished=true);await pause(0);
  check(ACTIVE_DM?.id===cid&&MSGS.has(privateId)&&document.querySelector('#m'+privateId),'private chat paints without waiting for network');
  check(!MSGS.has(publicId)&&directFinished,'private cache is isolated and navigation resolves immediately');
  const back=selectTab('feed');await pause(0);
  check(!ACTIVE_DM&&MSGS.has(publicId)&&!MSGS.has(privateId),'returning to public feed is immediate and scoped');
  // Release the private response after leaving it: it must not overwrite the public feed.
  hold=false;release();await Promise.all([direct,back]);await pause(200);
  check(!ACTIVE_DM&&MSGS.has(publicId)&&!MSGS.has(privateId),'late private response cannot replace the current public view');
  hold=true;const away=setProject(other);await pause(0);const home=setProject(main);await pause(0);
  check(ME.project.slug===main&&MSGS.has(publicId),'rapid project switches paint the final selection immediately');
  hold=false;release();await Promise.all([away,home]);await pause(200);
  check(ME.project.slug===main&&PROJECT===main&&MSGS.has(publicId)&&!MSGS.has(secondId),'late project metadata cannot overwrite the final selection');
  const oldId=ME.project.id;
  const oldPageKeys=[...CHAT_PAGES.keys()].filter(key=>JSON.parse(key)[1]===oldId);
  const projects=PROJECTS;PROJECTS=PROJECTS.filter(p=>p.id!==oldId);pruneChatCache();
  check(oldPageKeys.every(key=>!CHAT_PAGES.has(key)),'membership removal evicts its cached history');PROJECTS=projects;
  check(CHAT_BYTES<=4*1024*1024&&CHAT_PAGES.size<=24,'history cache stays bounded');
  clearChatCache();check(!CHAT_PAGES.size&&!CHAT_META.size&&!CHAT_BYTES,'account reset clears every cached conversation');
  check(!Object.keys(localStorage).some(key=>key.startsWith('tg_chat_cache')),'message cache is not persisted to browser storage');
  localStorage.setItem('tg_language','en');
  await original('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){hold=false;release();await original('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class ChatSwitchBrowserTest(unittest.TestCase):
    def test_cached_navigation_and_late_responses(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
