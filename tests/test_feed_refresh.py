"""Real browser regressions for refresh scroll anchors and delayed read responses."""
import unittest
import test_direct_browser as browser

PROBE=r'''
(async()=>{
const pause=ms=>new Promise(r=>setTimeout(r,ms)),failures=[];
const check=(ok,label)=>{if(!ok)failures.push(label)};
try{
 for(let i=0;i<150&&!ME?.project;i++)await pause(30);
 window.fixtureVisibility='hidden';Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>fixtureVisibility});
 READ_OBSERVER.disconnect();clearTimeout(READ_TIMER);READ_TIMER=null;READ_IDS.clear();
 const realApi=api;
 const actor=async data=>{const r=await fetch('/api/messages',{method:'POST',headers:{Authorization:'Bearer '+FIXTURE.agent_token,'Content-Type':'application/json','X-Telegent-Project':'main'},body:JSON.stringify(data)});return (await r.json()).id};
 const ids=[];for(let i=0;i<40;i++)ids.push(await actor({body:'Visible layout fixture '+i+' '+('line content '.repeat(24))}));
 await reload();SC.scrollTop=SC.scrollHeight*.45;await pause(40);
 const anchor=()=>{const top=SC.getBoundingClientRect().top;const n=[...$('feed').querySelectorAll('[data-msg]')].find(n=>n.getBoundingClientRect().bottom>top+2);return {id:n?.id,y:n?.getBoundingClientRect().top-top}};
 const before=anchor();
 await realApi('POST','/api/messages/'+ids[0]+'/reaction',{emoji:'🔥',active:true});await refresh();
 const after=anchor();check(after.id===before.id&&Math.abs(after.y-before.y)<2,'refresh changed visible message anchor');
 const query=filterQuery();query.set('limit','1000');if(OLDEST)query.set('after',OLDEST-1);
 const staleMessages=await realApi('GET','/api/messages?'+query),staleProjects=await realApi('GET','/api/projects');
 let releaseMessages,releaseProjects;
 const messageGate=new Promise(r=>releaseMessages=r),projectGate=new Promise(r=>releaseProjects=r);
 let captureMessages=true,captureProjects=true;
 api=async(...args)=>{if(captureMessages&&args[0]==='GET'&&args[1].startsWith('/api/messages?')){captureMessages=false;await messageGate;return staleMessages}if(captureProjects&&args[0]==='GET'&&args[1]==='/api/projects'){captureProjects=false;await projectGate;return staleProjects}return realApi(...args)};
 const oldRefresh=refresh(),oldProjects=loadProjects();
 const latest=Math.max(...ids);
 const read=await realApi('POST','/api/receipts',{kind:'read',ids,seen_upto:latest});
 ME.project.read_upto=read.read_upto;for(const id of ids)if(MSGS.has(id))MSGS.get(id).read=true;
 await loadProjects();
 releaseMessages();await oldRefresh;
 check(ids.every(id=>!MSGS.has(id)||MSGS.get(id).read),'late message response reverted confirmed reads');
 releaseProjects();await oldProjects;
 check(ME.project.read_upto>=latest,'late project response reverted read cursor');
 api=realApi;
 // A response for the previous filter cannot replace the newly opened view.
 let releaseFilter;
 const filterGate=new Promise(r=>releaseFilter=r);let captureFilter=true;
 api=async(...args)=>{if(captureFilter&&args[0]==='GET'&&args[1].startsWith('/api/messages?')){captureFilter=false;await filterGate;return staleMessages}return realApi(...args)};
 const previousView=refresh();$('q').value='absent-filter-fixture';await reload();
 releaseFilter();await previousView;
 check(MSGS.size===0,'previous filter response replaced current view');
 api=realApi;$('q').value='';await reload();
 // Exercise the HTTP page boundary without a thousand fixture writes.
 const template=[...MSGS.values()][0],epoch=FEED_EPOCH;
 const many=Array.from({length:1001},(_,i)=>({...template,id:1000000+i,thread_id:1000000+i,body:'Pagination fixture',subject:'Pagination fixture',read:true,receipts:[]}));
 api=async(...args)=>{
   if(args[0]==='GET'&&args[1].startsWith('/api/messages?')){
     const q=new URLSearchParams(args[1].split('?')[1]);
     if(q.has('after')&&q.get('limit')==='1000')return {...staleMessages,last_id:1001000,messages:many.filter(m=>m.id>+q.get('after')).slice(0,1000)};
   }
   return realApi(...args);
 };
 await refresh();
 check(MSGS.size===1001&&MSGS.has(1000000)&&MSGS.has(1001000),'refresh truncated messages at page boundary');
 check(FEED_EPOCH===epoch,'page boundary forced navigation reset');
 api=realApi;
 await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:!failures.length,failures,viewport:VIEWPORT})});
}catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,failures,viewport:VIEWPORT})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(), 'Headless Firefox is required')
class FeedRefreshTest(unittest.TestCase):
    def test_reading_position_and_confirmed_reads_survive_refresh(self):
        old=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE=browser.RESTORE_PROBE=PROBE
        browser.EXTRA_PROBE=None
        try:
            browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:
            browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=old

if __name__=='__main__': unittest.main(verbosity=2)
