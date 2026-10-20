"""Real desktop/mobile composition, public delivery, drafts and presence in RU/EN."""
import unittest
import test_direct_browser as browser

PROBE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<300;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
 const input=text=>{const t=$('cBody');t.focus();t.value=text;t.setSelectionRange(text.length,text.length);t.dispatchEvent(new Event('input'));};
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  resetCompose();
  for(const text of ['mail@alice','`@alice','```\n@alice','> @alice','    @alice','https://example.org/@alice']){
    input(text);check($('mentionList').classList.contains('hidden'),'no suggestions in email, code, quotes or URL: '+text);
  }
  input('`code` @alice');check(MENTION_OPTIONS.length===1,'completed inline code does not suppress a later ordinary mention');
  input('@');check(MENTION_OPTIONS.length===2,'current project offers its two agents, not self or foreign members');
  check($('mentionList').scrollHeight>0&&$('cBody').getAttribute('aria-expanded')==='true','accessible suggestions visible');
  const rect=$('mentionList').getBoundingClientRect();check(rect.left>=0&&rect.right<=innerWidth,'suggestions fit on mobile');
  $('cBody').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true,cancelable:true}));
  check(MENTION_INDEX===1,'keyboard selection moves');
  $('cBody').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  check($('mentionList').classList.contains('hidden'),'Escape dismisses suggestions');
  input('@alice-a');
  $('cBody').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
  check($('cBody').value==='@alice-agent '&&!SENDING,'Enter chooses participant without sending');
  check(MENTION_BINDINGS['alice-agent']==='alice-agent','chosen mention binds stable identity');
  const draft=$('cBody').value;
  await setProject(FIXTURE.second);await setProject('main');await wait(()=>NAV_READY&&ME.users.length===3);
  check($('cBody').value===draft&&MENTION_BINDINGS['alice-agent']==='alice-agent','mention and stable binding survive project draft switch');
  input(draft+'and @bob');
  $('mentionList').querySelector('button').click();
  check($('cBody').value==='@alice-agent and @bob-agent ','pointer selects second mention');
  $('cType').value='question';const body=$('cBody').value+'fixture '+VIEWPORT+' '+localStorage.getItem('tg_language');input(body);
  $('sendBtn').click();await wait(()=>!SENDING&&[...MSGS.values()].some(m=>m.body===body));
  const msg=[...MSGS.values()].find(m=>m.body===body);
  check(JSON.stringify(msg.to_ids)===JSON.stringify(['alice-agent','bob-agent']),'server stores both addressees in the public chat');
  check(!msg.conversation_id,'mention leaves the message public');
  check(document.querySelectorAll('#m'+msg.id+' .mention').length===2,'both resolved mentions highlighted');
  const mixed=(await api('POST','/api/messages',{body:'@alice-agent and `@alice-agent` and mail@alice-agent',to_ids:['alice-agent']})).id;
  await refresh();check(document.querySelectorAll('#m'+mixed+' .mention').length===1,'code and email stay plain even alongside a resolved mention');
  await showMember('alice-agent');
  check($('infoDialog').textContent.includes(tr('недавно активен')),'participant card uses recent authenticated activity');
  check($('infoDialog').textContent.includes(tr('нет активного слушателя')),'recent activity does not claim a listener');
  closeInfo();
  check(presenceText({kind:'agent',last_seen:'2026-01-02T12:00:00+07:00',last_check:'2026-01-01T12:00:00+07:00'})===tr('был ')+shortTime('2026-01-02T12:00:00+07:00'),'expired activity uses the newest timestamp, not old inbox check');
  await newDirect('alice-agent');await wait(()=>ACTIVE_DM);
  input('@');check(MENTION_OPTIONS.length===1&&MENTION_OPTIONS[0].id==='alice-agent','private suggestions offer only the conversation peer');
  input('@bob-agent private fixture');$('sendBtn').click();await wait(()=>!SENDING&&[...MSGS.values()].some(m=>m.body==='@bob-agent private fixture'));
  const privateMsg=[...MSGS.values()].find(m=>m.body==='@bob-agent private fixture');
  check(privateMsg.to_ids.length===1&&privateMsg.to_ids[0]==='alice-agent'&&!privateMsg.mentions.length,'third-party text does not change private delivery');
  check(!document.documentElement.scrollWidth||document.documentElement.scrollWidth<=innerWidth,'no horizontal overflow');
  localStorage.setItem('tg_language','en');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class MentionsBrowserTest(unittest.TestCase):
    def test_mentions_and_presence(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
