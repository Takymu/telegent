"""Native push enrollment handshake and authenticated message navigation, desktop/mobile RU/EN."""
import unittest
import test_direct_browser as browser

PROBE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<300;i++){if(fn())return;await pause(25)}throw Error('render timeout')};
 const routes=[];
 const click=HTMLAnchorElement.prototype.click;
 HTMLAnchorElement.prototype.click=function(){if(this.href.startsWith('telegent-push:')){routes.push(this.href);return;}return click.call(this)};
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  const agent=navigator.userAgent;
  Object.defineProperty(navigator,'userAgent',{configurable:true,value:agent+' telegent-android/0.1.3'});
  check(!nativePushSupported(),'older APK keeps its existing behavior');
  Object.defineProperty(navigator,'userAgent',{configurable:true,value:agent+' telegent-android/0.1.4'});
  check(nativePushSupported(),'native push integration detected');
  await nativePushSetup();
  check(nativePushConfig()?.configured===false,'missing provider configuration is explicit, not false success');
  check(routes.includes('telegent-push://sync'),'authenticated page signals native readiness');
  const nonce='0123456789abcdef0123456789abcdef';
  await nativePushEnroll('fixture-'+nonce,nonce+nonce,nonce);
  check(routes.at(-1)==='telegent-push://result/'+nonce+'/error','server refusal returns a nonce-bound failure');
  const count=routes.length;await nativePushEnroll('bad','bad','invalid');
  check(routes.length===count,'invalid callback nonce does not issue enrollment');
  const cid=(await api('POST','/api/conversations',{peer_id:'alice-agent'})).conversation.id;
  const privateId=(await api('POST','/api/messages',{body:'native private navigation fixture',conversation_id:cid})).id;
  await selectTab('feed');
  window.NATIVE_PUSH_OPEN=privateId;await nativePushFlush();
  check(ACTIVE_DM?.id===cid&&MSGS.has(privateId)&&!MSGS.has(FIXTURE.general),'notification opens only the authorized private conversation');
  check(window.NATIVE_PUSH_OPEN===null&&routes.at(-1)==='telegent-push://opened/'+privateId,'pending notification is consumed after navigation');
  window.NATIVE_PUSH_OPEN=FIXTURE.general;await nativePushFlush();
  check(!ACTIVE_DM&&MSGS.has(FIXTURE.general)&&!MSGS.has(privateId),'public notification leaves the private view and its content');
  const second=(await api('POST','/api/messages?project='+FIXTURE.second,{body:'native other project fixture'})).id;
  window.NATIVE_PUSH_OPEN=second;await nativePushFlush();
  check(PROJECT===FIXTURE.second&&MSGS.has(second)&&!MSGS.has(privateId),'project switch follows the authenticated message response');
  const previous=PROJECT;window.NATIVE_PUSH_OPEN=99999999;await nativePushFlush();
  check(PROJECT===previous&&window.NATIVE_PUSH_OPEN===null,'unavailable notification cannot open a project and does not loop forever');
  check(routes.every(url=>!url.includes('?')&&!url.includes('Bearer')&&!url.includes('native%20')),'native routes carry no text, destination or account bearer');
  nativePushCommand('logout');check(routes.at(-1)==='telegent-push://logout','logout disables native reception');
  localStorage.setItem('tg_language','en');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,checks,viewport:VIEWPORT})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,checks,viewport:VIEWPORT})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class PushBrowserTest(unittest.TestCase):
    def test_enrollment_and_message_navigation(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
