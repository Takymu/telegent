"""Download action is available and localized on desktop and mobile, before and after login."""
import unittest
import test_direct_browser as browser

PROBE = r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 try{
  for(let i=0;i<200&&!(typeof ME!=='undefined'&&ME?.project&&NAV_READY);i++)await pause(30);
  const expected='https://github.com/Takymu/telegent/releases/latest/download/telegent.apk';
  const login=document.querySelector('#login .app-download');
  check(login?.href===expected,'download available without API credentials on login screen');
  check(login.textContent===tr('Скачать приложение для Android'),'login action translated');
  $('settingsBtn').click();
  check($('infoDialog').open,'settings opens');
  const link=document.querySelector('#infoBody .app-download');
  check(link?.href===expected,'settings links to stable official APK');
  check(link.textContent===tr('Скачать приложение для Android'),'settings action translated');
  const box=link.getBoundingClientRect();
  check(box.height>=44&&box.width>100,'download action has reachable touch target');
  check($('infoBody').scrollWidth<=$('infoBody').clientWidth,'download card fits mobile settings');
  let clicked=false;link.addEventListener('click',e=>{e.preventDefault();clicked=true},{once:true});link.click();
  check(clicked,'download action is clickable');closeInfo();
  localStorage.setItem('tg_language','en');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,checks})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class AppDownloadBrowserTest(unittest.TestCase):
    def test_download_actions(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
