"""Native sharing buttons and payload isolation in real desktop/mobile Firefox."""
import unittest
import test_direct_browser as browser

PROBE = r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<300;i++){if(fn())return;await pause(25)}throw Error('render timeout')};
 const routes=[];
 document.addEventListener('click',e=>{const link=e.target.closest('a[data-native-share]');if(link){e.preventDefault();routes.push(link.href)}},true);
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  const agent=navigator.userAgent;
  Object.defineProperty(navigator,'userAgent',{configurable:true,value:agent+' telegent-android/0.1.3'});
  const body='Share **original** text '+VIEWPORT;
  const id=(await api('POST','/api/messages',{body,attachments:[{name:'fixture.txt',content_b64:btoa('share fixture')}] })).id;
  await refresh();const attachment=MSGS.get(id).attachments[0];
  openMessageContext($('m'+id));
  const share=document.querySelector('#menu [data-act="sharetext"]');
  check(share?.textContent.includes(tr('Поделиться')),'native message menu includes localized sharing');share.click();
  check(routes.at(-1)==='telegent-share://message/'+id,'message action sends only an opaque ID to Android');
  check(nativeSharePayload('message',id).text===body,'shared text preserves original body without adding credentials or metadata');
  const file=document.querySelector('#m'+id+' [data-act="sharefile"]');
  check(file&&file.getBoundingClientRect().height>=40,'file has a reachable sharing button');file.click();
  await wait(()=>routes.at(-1)==='telegent-share://attachment/'+attachment.id);
  check(nativeSharePayload('attachment',attachment.id).name==='fixture.txt','native file payload has only chosen attachment identity and name');
  check(routes.every(url=>!url.includes('token')&&!url.includes('Share')&&!url.includes('?')),'bridge URLs carry no content or authentication');
  const cid=(await api('POST','/api/conversations',{peer_id:'alice-agent'})).conversation.id;
  const privateId=(await api('POST','/api/messages',{body:'private share fixture',conversation_id:cid})).id;
  const secret=(await api('POST',`/api/conversations/${cid}/secrets`,{value:'private fixture value',expires_in:300,idem_key:'share-fixture-'+VIEWPORT+'-'+Date.now()})).id;
  await loadConversations();await openDirect(cid);
  check(nativeSharePayload('message',id)===null&&nativeSharePayload('attachment',attachment.id)===null,'private view cannot share a public view payload by guessing ID');
  check(nativeSharePayload('message',privateId).text==='private share fixture','explicitly selected private text can be shared');
  check(nativeSharePayload('message',secret)===null&&!actionsFor(MSGS.get(secret)).some(a=>a.act==='sharetext'),'secret envelopes do not enter native sharing');
  await selectTab('feed');check(nativeSharePayload('message',privateId)===null,'public view cannot expose a cached private payload');
  Object.defineProperty(navigator,'userAgent',{configurable:true,value:agent+' telegent-android/0.1.2'});
  check(!nativeSharing()&&!actionsFor(MSGS.get(id)).some(a=>a.act==='sharetext'),'older APKs are not shown unsupported native actions');
  localStorage.setItem('tg_language','en');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class SharingBrowserTest(unittest.TestCase):
    def test_native_actions_and_payload_boundaries(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
