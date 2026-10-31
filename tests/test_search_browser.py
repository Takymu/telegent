"""Header search, optional filters and private scope in desktop/mobile Firefox."""
import unittest

import test_direct_browser as browser


PROBE = r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<220;i++){if(fn())return;await pause(30)}throw Error('render timeout')};
 const type=text=>{$('q').value=text;$('q').dispatchEvent(new Event('input',{bubbles:true}))};
 try{
   await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
   const search=$('filtersBtn'),settings=$('settingsBtn');
   check(search.closest('header')&&search.nextElementSibling===settings,'search sits beside settings in chat header');
   const a=search.getBoundingClientRect(),b=settings.getBoundingClientRect();
   check(a.width>=40&&a.height>=40&&Math.abs(a.top-b.top)<2,'search has a reachable header target on both viewports');
   check(search.getAttribute('aria-label')===tr('Поиск'),'header search label is localized');
   check(getComputedStyle($('filters')).display==='none','search is initially closed');
   search.click();
   check($('filters').classList.contains('open')&&document.activeElement===$('q'),'header opens and focuses search');
   check($('filterOptions').classList.contains('hidden')&&!$('fType').getBoundingClientRect().height,'advanced filters are not shown by default');
   check($('advancedFiltersBtn').getAttribute('aria-expanded')==='false','filter disclosure announces its collapsed state');
   const text='header-search-'+VIEWPORT+'-'+Date.now();
   const info=(await api('POST','/api/messages',{body:text+' info',type:'info'})).id;
   const question=(await api('POST','/api/messages',{body:text+' question',type:'question'})).id;
   type(text);
   await wait(()=>MSGS.size===2&&MSGS.has(info)&&MSGS.has(question));
   check(!MSGS.has(FIXTURE.general),'plain search actually filters chat history');
   $('advancedFiltersBtn').click();
   check(!$('filterOptions').classList.contains('hidden')&&$('fType').getBoundingClientRect().height>0,'small filter button expands options');
   check($('advancedFiltersBtn').getAttribute('aria-expanded')==='true','filter disclosure announces its expanded state');
   $('fType').value='question';$('fType').dispatchEvent(new Event('change',{bubbles:true}));
   await wait(()=>MSGS.size===1&&MSGS.has(question));
   check($('searchCount').textContent==='2','header badge includes search and advanced filter');
   $('advancedFiltersBtn').click();
   check($('filterOptions').classList.contains('hidden')&&$('fType').value==='question','collapsing filters preserves their values');
   $('closeFilters').click();
   check(!$('filters').classList.contains('open')&&$('filterSummary').textContent.includes(text),'closing search preserves visible active conditions');
   search.click();
   check($('q').value===text&&$('filterOptions').classList.contains('hidden'),'reopening search stays compact and preserves query');
   document.querySelector('#filterSummary [data-ux="reset-filters"]').click();
   await wait(()=>!hasFilters()&&MSGS.has(FIXTURE.general));
   $('closeFilters').click();
   document.dispatchEvent(new KeyboardEvent('keydown',{key:'k',ctrlKey:true,bubbles:true,cancelable:true}));
   await wait(()=>$('filters').classList.contains('open')&&document.activeElement===$('q'));
   check($('filterOptions').classList.contains('hidden'),'Ctrl+K opens plain search');
   document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
   check(!$('filters').classList.contains('open')&&search.getAttribute('aria-expanded')==='false','Escape closes search and updates button state');
   const filename='Отчёт-file-search-'+VIEWPORT+'-'+Date.now()+'.PDF';
   const attachment={name:filename,content_b64:btoa('fixture')};
   const fileMessage=(await api('POST','/api/messages',{body:'Neutral fixture',attachments:[attachment,attachment]})).id;
   search.click();type(filename.toLowerCase());
   await wait(()=>MSGS.size===1&&MSGS.has(fileMessage));
   check(MSGS.get(fileMessage).attachments.length===2,'filename search finds one message despite multiple matching attachments');
   resetFilters();setSearchOpen(false);
   const cid=(await api('POST','/api/conversations',{peer_id:'alice-agent'})).conversation.id;
   const privateText='private-search-'+Date.now();
   const publicId=(await api('POST','/api/messages',{body:privateText})).id;
   const privateId=(await api('POST','/api/messages',{body:privateText,conversation_id:cid})).id;
   await openDirect(cid);search.click();type(privateText);
   await wait(()=>MSGS.size===1&&MSGS.has(privateId));
   check(!MSGS.has(publicId)&&ACTIVE_DM?.id===cid,'header search stays inside the current private conversation');
   const dmFile=(await api('POST','/api/messages',{body:'Private file fixture',conversation_id:cid,attachments:[attachment]})).id;
   type(filename.toLowerCase());await wait(()=>MSGS.size===1&&MSGS.has(dmFile));
   check(!MSGS.has(fileMessage),'filename search also stays inside the selected private conversation');
   check(document.documentElement.scrollWidth<=innerWidth,'search and filters fit the viewport');
   check($('filters').scrollWidth<=$('filters').clientWidth,'search controls do not overflow on mobile');
   resetFilters();setSearchOpen(false);await selectTab('feed');
   localStorage.setItem('tg_language','en');
   await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
})();
'''


@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class SearchBrowserTest(unittest.TestCase):
    def test_header_search_and_optional_filters(self):
        original = browser.PROBE, browser.RESTORE_PROBE, browser.EXTRA_PROBE
        browser.PROBE, browser.RESTORE_PROBE, browser.EXTRA_PROBE = PROBE, PROBE, None
        try:
            browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:
            browser.PROBE, browser.RESTORE_PROBE, browser.EXTRA_PROBE = original


if __name__ == '__main__':
    unittest.main(verbosity=2)
