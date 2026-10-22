"""Real GIF upload, inline playback, download and conversation isolation in RU/EN Firefox."""
import base64
import unittest

import test_direct_browser as browser
from test_gifs import animated_gif


PROBE = r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<350;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  resetCompose();
  const bytes=Uint8Array.from(atob('GIF_FIXTURE'),c=>c.charCodeAt(0));
  const filename='reaction-'+VIEWPORT+'-'+Date.now()+'.GIF';
  await addFiles([new File([bytes],filename,{type:'image/gif'})]);
  check(!!$('cPending').querySelector('.file-thumb'),'composer previews an animated attachment');
  $('sendBtn').click();
  await wait(()=>!SENDING&&[...MSGS.values()].some(m=>m.attachments.some(a=>a.name===filename)));
  const message=[...MSGS.values()].find(m=>m.attachments.some(a=>a.name===filename));
  const aid=message.attachments[0].id;
  await wait(()=>$('m'+message.id)?.querySelector('.inline-image img')?.naturalWidth===40);
  const row=$('m'+message.id),img=row.querySelector('.inline-image img'),entry=INLINE_IMAGES.get(String(aid));
  check(row.querySelector('.gif-label').textContent==='GIF','animation has a compact GIF badge');
  check(entry.blob.type==='image/gif'&&entry.height===24,'actual GIF signature is checked before playback');
  check(img.src.startsWith('blob:')&&!img.src.includes(TOKEN),'preview URL contains no credentials');
  check(row.querySelector('.gif-card').getBoundingClientRect().right<=row.querySelector('.mmain').getBoundingClientRect().right+1,'GIF fits bubble on desktop and phone');
  const button=row.querySelector('.gif-download');
  check(button.getBoundingClientRect().width>=44&&button.getAttribute('aria-label')===tr('Скачать ')+filename,'download is accessible and localized');
  observeInlineImages();await pause(40);
  check(row.querySelector('.inline-image img')===img,'refreshing observers does not restart a playing GIF');
  row.querySelector('.inline-image').click();await wait(()=>$('infoBody').querySelector('img')?.naturalWidth===40);
  check(!!$('infoBody').querySelector('[data-act="dl"]'),'expanded GIF keeps original download');closeInfo();
  const original=await fetch(`/api/attachments/${aid}`,{headers:{Authorization:'Bearer '+TOKEN,'X-Telegent-Project':PROJECT}});
  const copy=new Uint8Array(await original.arrayBuffer());
  check(copy.length===bytes.length&&copy.every((b,i)=>b===bytes[i]),'download preserves both frames and looping bytes');
  const invalid=(await api('POST','/api/messages',{attachments:[{name:'fake.gif',content_b64:btoa('<svg onload="alert(1)"></svg>')}]})).id;
  await refresh();await wait(()=>$('m'+invalid)?.querySelector('.inline-image[data-act="dl"]'));
  check(!$('m'+invalid).querySelector('.inline-image img,.inline-image svg'),'an extension does not turn active content into an image');
  const cid=(await api('POST','/api/conversations',{peer_id:'alice-agent'})).conversation.id;
  await loadConversations();await openDirect(cid);
  check(!INLINE_IMAGES.has(String(aid))&&!document.querySelector(`[data-image="${aid}"]`),'private view releases public image data');
  const privateId=(await api('POST','/api/messages',{conversation_id:cid,attachments:[{name:'private.gif',content_b64:btoa(String.fromCharCode(...bytes))}]})).id;
  await refresh();await wait(()=>$('m'+privateId)?.querySelector('.inline-image img')?.naturalWidth===40);
  const privateAid=MSGS.get(privateId).attachments[0].id;
  await selectTab('feed');check(!INLINE_IMAGES.has(String(privateAid))&&!$('m'+privateId),'private GIF is not rendered into general chat');
  check(document.documentElement.scrollWidth<=innerWidth,'no horizontal overflow');
  localStorage.setItem('tg_language','en');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
})();
'''.replace('GIF_FIXTURE', base64.b64encode(animated_gif()).decode())


@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(), 'Headless Firefox is required')
class GifBrowserTest(unittest.TestCase):
    def test_gifs_in_public_and_private_chats(self):
        original = browser.PROBE, browser.RESTORE_PROBE, browser.EXTRA_PROBE
        browser.PROBE, browser.RESTORE_PROBE, browser.EXTRA_PROBE = PROBE, PROBE, None
        try:
            browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:
            browser.PROBE, browser.RESTORE_PROBE, browser.EXTRA_PROBE = original


if __name__ == '__main__':
    unittest.main(verbosity=2)
