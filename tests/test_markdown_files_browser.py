"""Real authenticated Markdown attachment previews, tab UX and chat isolation in RU/EN Firefox."""
import unittest
import test_direct_browser as browser

PROBE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<350;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
 const encode=text=>btoa(String.fromCharCode(...new TextEncoder().encode(text)));
 const send=async(name,text)=>{const mid=(await api('POST','/api/messages',{attachments:[{name,content_b64:encode(text)}]})).id;await refresh();return {mid,aid:MSGS.get(mid).attachments[0].id};};
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  const originalWidth=STATUS_WIDTH;
  const text='# Report\n\n**Bold** and [link](https://example.invalid/docs)\n\n| Name | Value |\n| :--- | ---: |\n| escaped\\|pipe | `a|b` |\n\n```js\nconst x = "<script>";\n```\n\n<img src="/unexpected-preview-image" onerror="window.__document_xss=1">\n\n[bad](javascript:alert(1))';
  const first=await send('report-'+VIEWPORT+'.MD',text);
  const card=$('m'+first.mid).querySelector('[data-doc-open]');check(!!card,'Markdown card opens a reader');card.click();
  await wait(()=>DOCUMENTS.get(String(first.aid))?.text===text);
  check(DOCUMENT_OPEN&&!$('documentReader').classList.contains('hidden'),'preview opens in the right panel');
  check($('documentBody').querySelector('h1')?.textContent==='Report','heading rendered');
  check($('documentBody').querySelector('strong')?.textContent==='Bold','inline formatting rendered');
  check($('documentBody').querySelectorAll('td').length===2,'table columns rendered');
  check($('documentBody').querySelector('td').textContent==='escaped|pipe'&&$('documentBody').querySelector('td code').textContent==='a|b','escaped and code pipes preserve cells');
  check(!$('documentBody').querySelector('script,img,svg,iframe,object,[onerror]')&&!window.__document_xss,'raw HTML and images remain inactive');
  check([...$('documentBody').querySelectorAll('a')].every(a=>a.protocol==='https:'&&a.rel.includes('noreferrer')),'preview links are sanitized');
  check($('documentDownload').dataset.att===String(first.aid),'original download stays available');
  $('documentSource').click();check($('documentBody').querySelector('pre').textContent===text,'source is exact and escaped');
  $('documentRender').click();
  if(VIEWPORT==='desktop'){
    const pane=$('documentReader').getBoundingClientRect(),chat=document.querySelector('main').getBoundingClientRect();
    check(pane.left>=chat.right&&pane.width>=400,'reader sits beside a usable chat');
    statusWidth(480,true);check(STATUS_WIDTH===originalWidth,'reader width preference is independent of participant width');
  }else{
    const pane=$('documentReader').getBoundingClientRect();check(pane.left===0&&pane.width===innerWidth,'phone preview spans screen width');
    check(!readingFeed(),'covered phone chat is not marked read');
  }
  const second=await send('second.markdown','# Second file');await openDocument(second.aid);
  check($('documentTabs').querySelectorAll('[role=tab]').length===2,'several documents have separate tabs');
  $('document-tab-'+first.aid).click();check($('documentBody').querySelector('h1').textContent==='Report','tab switching restores the first file');
  $('documentTabs').dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true}));
  check(DOCUMENT_ACTIVE===String(second.aid),'tabs support keyboard navigation');
  await api('POST','/api/messages/'+second.mid+'/delete',{revision:MSGS.get(second.mid).revision});await refresh();
  check(!DOCUMENTS.has(String(second.aid))&&!$('documentBody').textContent.includes('Second file'),'deleted attachment preview is evicted');
  clearDocuments();check(!DOCUMENT_OPEN&&!DOCUMENTS.size&&STATUS_WIDTH===originalWidth,'closing releases tabs and restores participant panel');
  const big=(await api('POST','/api/messages',{attachments:[{name:'large.md',content_b64:btoa('# Large\n'+'x'.repeat(600000))}]})).id;
  await refresh();const bigAid=MSGS.get(big).attachments[0].id;await openDocument(bigAid);
  check(DOCUMENTS.get(String(bigAid)).truncated&&DOCUMENTS.get(String(bigAid)).text.length===200000,'large previews are bounded with an explicit notice');
  check(!!$('documentBody').querySelector('.document-warning'),'truncated preview is visibly labelled');
  clearDocuments();
  const invalid=(await api('POST','/api/messages',{attachments:[{name:'invalid.md',content_b64:btoa(String.fromCharCode(255,0))}]})).id;
  await refresh();await openDocument(MSGS.get(invalid).attachments[0].id);
  check(!!$('documentBody').querySelector('.err')&&!!$('documentBody').querySelector('[data-doc-action=retry]'),'encoding failure offers retry and original download');
  clearDocuments();
  const retryFile=await send('retry.md','# Recovered');const originalFetch=window.fetch;let fail=true;
  window.fetch=(url,options)=>String(url).includes('/api/attachments/'+retryFile.aid)&&fail?(fail=false,Promise.resolve(new Response('{}',{status:503}))):originalFetch(url,options);
  await openDocument(retryFile.aid);check(!!$('documentBody').querySelector('.err'),'network refusal is shown');
  $('documentBody').querySelector('[data-doc-action=retry]').click();await wait(()=>DOCUMENTS.get(String(retryFile.aid))?.text==='# Recovered');
  check(!$('documentBody').querySelector('.err'),'retry replaces the failed response');window.fetch=originalFetch;clearDocuments();
  const utf16Bytes=[255,254];for(const c of '# UTF16 document')utf16Bytes.push(c.charCodeAt(0),0);
  const utf16=(await api('POST','/api/messages',{attachments:[{name:'utf16.md',content_b64:btoa(String.fromCharCode(...utf16Bytes))}]})).id;
  await refresh();await openDocument(MSGS.get(utf16).attachments[0].id);
  check($('documentBody').querySelector('h1')?.textContent==='UTF16 document','UTF-16 BOM is decoded');clearDocuments();
  const delayed=await send('delayed.md','# Private late response');
  const realFetch=window.fetch;let release;
  window.fetch=(url,options)=>String(url).includes('/api/attachments/'+delayed.aid)?new Promise(resolve=>{release=()=>resolve(new Response('# Private late response',{status:200}));}):realFetch(url,options);
  const pending=openDocument(delayed.aid);await wait(()=>!!release);
  const cid=(await api('POST','/api/conversations',{peer_id:'alice-agent'})).conversation.id;await loadConversations();await openDirect(cid);
  release();await pending;window.fetch=realFetch;
  check(!DOCUMENT_OPEN&&!DOCUMENTS.size&&!$('documentBody').textContent,'late response cannot restore a preview in a different chat');
  const privateMid=(await api('POST','/api/messages',{conversation_id:cid,attachments:[{name:'private.md',content_b64:encode('# Private report')}]})).id;
  await refresh();const privateAid=MSGS.get(privateMid).attachments[0].id;await openDocument(privateAid);
  check($('documentBody').querySelector('h1')?.textContent==='Private report','private file opens within its own conversation');
  await selectTab('feed');await openDocument(privateAid);check(!DOCUMENTS.size&&!$('documentBody').textContent,'a private attachment ID cannot reopen its cached preview in public chat');
  await selectTab('feed');await openDocument(first.aid);await setProject(FIXTURE.second);
  check(!DOCUMENTS.size&&!DOCUMENT_OPEN,'changing project clears all preview content');
  check(document.documentElement.scrollWidth<=innerWidth,'no horizontal page overflow');
  await setProject('main');await openDocument(first.aid);
  localStorage.setItem('tg_language','en');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,checks,viewport:VIEWPORT})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,checks,viewport:VIEWPORT})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class MarkdownFilesBrowserTest(unittest.TestCase):
    def test_reader_tabs_content_bounds_and_scope(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
