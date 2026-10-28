"""Real shared-material tabs and jump-to-latest on desktop/mobile RU/EN."""
import unittest
import test_direct_browser as browser

PROBE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<350;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
 const encode=text=>btoa(String.fromCharCode(...new TextEncoder().encode(text)));
 const file=async(name,text,extra={})=>(await api('POST','/api/messages',{body:'Shared fixture',attachments:[{name,content_b64:encode(text)}],...extra})).id;
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  const png='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO8kAAAAASUVORK5CYII=';
  const photo=(await api('POST','/api/messages',{body:'Photo fixture',attachments:[{name:'fixture.png',content_b64:png}]})).id;
  const md=await file('report-'+VIEWPORT+'.md','# Shared report\n\n**Fixture** text');
  await api('POST','/api/messages',{body:'[docs](https://example.invalid/docs?a=1&b=2) javascript:alert(1)'});
  await refresh();$('sharedBtn').click();await wait(()=>SHARED_LIBRARY?.items.length);
  check(SHARED_LIBRARY.kind==='photos'&&!!$('sharedItems').querySelector('.shared-photo'),'photo gallery opens from the chat header');
  check(!readingFeed(),'gallery does not read covered messages');
  await wait(()=>$('sharedItems').querySelector('img')?.naturalWidth>0);
  check($('sharedItems').querySelector('img').src.startsWith('blob:'),'authenticated thumbnail uses a local blob');
  document.querySelector('[data-kind="files"]').click();await wait(()=>SHARED_LIBRARY?.kind==='files'&&SHARED_LIBRARY.items.some(i=>i.message_id===md));
  $('sharedSearch').value='report-'+VIEWPORT;$('sharedSearch').dispatchEvent(new Event('input',{bubbles:true}));
  await wait(()=>SHARED_LIBRARY.query==='report-'+VIEWPORT&&$('sharedItems').textContent.includes('report-'+VIEWPORT));
  const item=SHARED_LIBRARY.items.find(i=>i.message_id===md);
  MSGS.delete(md); // An old attachment can be opened without loading its message into the feed.
  await openSharedFile(item.attachment.id,md);
  await wait(()=>DOCUMENT_OPEN&&$('documentBody').querySelector('h1')?.textContent==='Shared report');
  check(!$('infoDialog').open,'Markdown opens in the existing reader');clearDocuments();
  await showShared();document.querySelector('[data-kind="links"]').click();await wait(()=>SHARED_LIBRARY.kind==='links'&&SHARED_LIBRARY.items.some(i=>i.links));
  check([...$('sharedItems').querySelectorAll('a')].every(a=>a.protocol==='https:'&&a.rel.includes('noreferrer')),'links remain HTTP(S) and do not interpret script URLs');
  check(!$('sharedItems').querySelector('iframe,script'),'no active embeds');
  const normal=api;let release;
  api=async(method,path,...rest)=>path.startsWith('/api/shared')?new Promise(r=>release=r):normal(method,path,...rest);
  const pending=loadShared(false);await wait(()=>!!release);showInfo('Other view','<p>Unchanged</p>');release({items:[],next_before:null,revision:'late'});await pending;
  check($('infoBody').textContent==='Unchanged','late results cannot overwrite another view');api=normal;closeInfo();
  const dm=(await api('POST','/api/conversations',{peer_id:'alice-agent'})).conversation.id;
  const secret=await file('private-'+VIEWPORT+'.md','Private fixture',{conversation_id:dm});
  await openDirect(dm);await showShared();document.querySelector('[data-kind="files"]').click();
  await wait(()=>SHARED_LIBRARY?.items.some(i=>i.message_id===secret));
  check(!SHARED_LIBRARY.items.some(i=>i.message_id===md),'private materials do not include general chat files');
  await selectTab('feed');check(!SHARED_LIBRARY&&!$('infoDialog').open,'switching chats clears material metadata');
  const long=(await api('POST','/api/messages',{body:Array(90).fill('Long fixture paragraph.').join('\n\n')})).id;
  const latest=(await api('POST','/api/messages',{body:'Latest fixture'})).id;await reload();
  UNSEEN=0;SC.scrollTop=0;SC.dispatchEvent(new Event('scroll'));await wait(()=>!$('newBtn').classList.contains('hidden'));
  check(!nearBottom(),'scroll alone shows a down arrow without new incoming messages');
  await $('newBtn').onclick();check(nearBottom()&&MSGS.has(latest),'arrow reaches current latest messages');
  THREAD=long;paintTab();await reload();updateNewBtn();check(!$('newBtn').classList.contains('hidden'),'thread offers return to latest');
  await $('newBtn').onclick();check(!THREAD&&nearBottom()&&MSGS.has(latest),'arrow exits the thread and reloads the end');
  check(document.documentElement.scrollWidth<=innerWidth,'header and gallery fit mobile width');
  await showShared();await login(TOKEN);
  check(!SHARED_LIBRARY&&!$('infoDialog').open,'re-authentication clears previous material metadata');
  localStorage.setItem('tg_language','en');await showShared();
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,checks,viewport:VIEWPORT})});
 }catch(error){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:error.message,stack:error.stack,checks,viewport:VIEWPORT})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class SharedBrowserTest(unittest.TestCase):
    def test_shared_materials_and_latest_arrow(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
