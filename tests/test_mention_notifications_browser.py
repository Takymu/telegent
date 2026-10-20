"""Mention emphasis and opt-in muted-bell exceptions; OS permission stays authoritative."""
import unittest
import test_direct_browser as browser

PROBE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<300;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  const res=await appFetch('/api/messages?project=main',{method:'POST',headers:{Authorization:'Bearer '+FIXTURE.agent_token,'Content-Type':'application/json'},body:JSON.stringify({body:'Hi @alice and @bob-agent, fixture '+VIEWPORT})});
  check(res.ok,'real agent message sent');const id=(await res.json()).id;
  await refresh();
  check($('m'+id).querySelectorAll('.mention').length===2,'both mentions have emphasis');
  const self=$('m'+id).querySelector('.mention-me'),other=$('m'+id).querySelector('.mention:not(.mention-me)');
  check(self?.dataset.account===ME.id&&other?.dataset.account==='bob-agent','self emphasis uses stable identity');
  for(const theme of ['light','dark'])for(const colors of ['default','telegram']){
    document.documentElement.dataset.theme=theme;document.documentElement.dataset.colors=colors;
    check(getComputedStyle(self).backgroundColor!==getComputedStyle(other).backgroundColor,'self background distinct in '+theme+'/'+colors);
    check(getComputedStyle(self).color!==getComputedStyle(self).backgroundColor,'self tag remains legible in '+theme+'/'+colors);
  }
  const Original=window.Notification,originalRegister=ntfRegister,originalFocus=document.hasFocus;
  let permission='granted',requested=0,shown=[];
  window.Notification=class{static get permission(){return permission;}static requestPermission(callback){requested++;permission='granted';callback?.(permission);return Promise.resolve(permission);}constructor(title,options){shown.push({title,options});}close(){}};
  ntfRegister=async()=>{};NTF.reg=null;
  Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});document.hasFocus=()=>false;
  st.set('tg_notify','0');st.set(ntfMentionKey(),null);
  const base={...MSGS.get(id),id:1000000,read:false,acked:false,conversation_id:null,approvals:[],needs_approval:false};
  ntfNew([base]);check(!shown.length,'muted bell remains silent before explicit opt-in');
  showSettings();let option=$('infoBody').querySelector('[data-mention-notify]');
  check(option&&!option.checked,'settings exception starts disabled');
  check($('infoBody').textContent.includes(tr('Уведомлять об @упоминаниях при выключенном колокольчике')),'exception label localized');
  option.click();await wait(()=>ntfMentionsOn());
  check(st.get('tg_notify')==='0'&&$('infoBody').querySelector('[data-mention-notify]').checked,'exception does not enable other notifications');
  check($('infoBody').textContent.includes(tr('Только @упоминания')),'settings state describes mentions-only mode');
  ntfNew([base]);check(shown.length===1&&shown[0].title.includes(tr('Тебя упомянули')),'own mention notifies despite muted bell');
  ntfNew([{...base,id:1000001,mentions:[],to:[ME.name],to_ids:[ME.id]}]);check(shown.length===1,'ordinary direct addressing does not use the mention exception');
  ntfNew([{...base,id:1000002,mentions:[{id:'bob-agent'}]}]);check(shown.length===1,'mention of somebody else stays muted');
  for(const change of [{read:true},{acked:true},{author_id:ME.id}])ntfNew([{...base,...change}]);
  check(shown.length===1,'read, acknowledged and own messages never notify');
  permission='denied';ntfNew([base]);check(shown.length===1,'blocked browser permission cannot be bypassed');
  permission='granted';st.set('tg_notify','1');ntfNew([{...base,mentions:[]}]);check(shown.length===2,'normal bell notification behavior remains');st.set('tg_notify','0');
  const me=ME;ME={...me,id:'another-account'};check(!ntfMentionsOn(),'exception does not leak to another account');ME=me;
  showSettings();$('infoBody').querySelector('[data-mention-notify]').click();await wait(()=>!ntfMentionsOn());
  ntfNew([base]);check(shown.length===2,'disabling the exception restores a fully muted bell');
  permission='default';showSettings();$('infoBody').querySelector('[data-mention-notify]').click();await wait(()=>ntfMentionsOn());
  check(requested===1,'enabling exception requests missing browser permission from the click');
  check($('infoBody').getBoundingClientRect().width<=innerWidth,'settings fit on mobile');
  st.set(ntfMentionKey(),null);st.set('tg_notify','0');window.Notification=Original;ntfRegister=originalRegister;document.hasFocus=originalFocus;delete document.visibilityState;closeInfo();
  localStorage.setItem('tg_language','en');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class MentionNotificationsBrowserTest(unittest.TestCase):
    def test_emphasis_and_muted_notification_exception(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
