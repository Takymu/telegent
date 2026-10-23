"""Transparent client marks in real RU/EN desktop/mobile chat surfaces."""
import unittest
import test_direct_browser as browser

PROBE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<300;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
 const actor=async(token,client,path,data)=>{
  const r=await fetch(path,{method:data?'POST':'GET',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json','X-Telegent-Project':'main','X-Telegent-Client':client},body:data?JSON.stringify(data):undefined});
  const body=await r.json();if(!r.ok)throw Error(body.error);return body;
 };
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  const human=document.createElement('div');human.innerHTML=avatar('alice','human',false,'codex');
  check(!human.querySelector('svg')&&human.textContent==='AL','human avatars retain initials despite a client hint');
  const unknown=document.createElement('div');unknown.innerHTML=avatar('codex-agent','agent');
  check(!unknown.querySelector('svg'),'name does not guess the agent client');
  unknown.innerHTML=avatar('<svg onload=alert(1)>','agent',false,'toString');
  check(!unknown.querySelector('svg'),'unknown metadata and display names cannot inject SVG');
  const invite=await api('POST','/api/invites',{kind:'agent',name:'logo-'+VIEWPORT+'-'+Date.now()});
  const joinedResponse=await fetch('/api/join',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:invite.code})});
  const joined=await joinedResponse.json();check(joinedResponse.ok,'second client joins a real isolated test account');
  await api('POST','/api/messages',{body:'Avatar separator fixture'});
  const codex=(await actor(FIXTURE.agent_token,'codex','/api/messages',{body:'Codex avatar fixture'})).id;
  const claude=(await actor(joined.token,'claude','/api/messages',{body:'Claude avatar fixture'})).id;
  await loadPresence();await refresh();
  for(const [id,client] of [[codex,'codex'],[claude,'claude']]){
   const av=$('m'+id).querySelector('.mav');
   check(av?.dataset.client===client&&!!av.querySelector('svg path'),'feed shows the reported client mark');
   check(getComputedStyle(av).backgroundColor==='rgba(0, 0, 0, 0)','logo background is transparent');
   check(av.getAttribute('aria-label').includes(client==='codex'?'Codex (ChatGPT)':'Claude Code'),'client has an accessible name');
   check(av.querySelector('svg').getBoundingClientRect().width===av.getBoundingClientRect().width,'vector scales within avatar bounds');
  }
  const claudeId=MSGS.get(claude).author_id;
  check(!!$('presence').querySelector(`.mav[data-account="${claudeId}"][data-client="claude"] svg`),'participant list uses the same logo');
  await selectTab('direct');await loadConversations();
  check(!!$('dmList').querySelector('.mav[data-account="alice-agent"][data-client="codex"] svg'),'private conversation choices show client marks');
  await newDirect(claudeId);await selectTab('direct');await loadConversations();
  check(!!$('dmList').querySelector(`.mav[data-account="${claudeId}"][data-client="claude"] svg`),'existing private conversation keeps the correct mark');
  await selectTab('feed');$('cBody').focus();$('cBody').value='@';$('cBody').setSelectionRange(1,1);$('cBody').dispatchEvent(new Event('input'));
  check($('mentionList').querySelectorAll('.mav.brand svg').length>=2,'mention suggestions reuse both marks');
  resetCompose();
  await actor(FIXTURE.agent_token,'claude','/api/me');await loadPresence();
  check($('m'+codex).querySelector('.mav').dataset.client==='claude','presence changes update the client mark');
  await actor(FIXTURE.agent_token,'codex','/api/me');await loadPresence();
  await api('POST','/api/rename',{account_id:claudeId,new:'renamed-'+VIEWPORT+'-'+Date.now()});
  await loadPresence();await refresh();
  check($('m'+claude).querySelector('.mav').dataset.client==='claude','rename preserves stable client identity');
  for(const theme of ['light','dark']){
   document.documentElement.dataset.theme=theme;
   check(getComputedStyle($('m'+codex).querySelector('.mav')).color===getComputedStyle(document.body).color,'ChatGPT mark follows text contrast in '+theme);
  }
  check(document.documentElement.scrollWidth<=innerWidth,'no horizontal overflow');
  localStorage.setItem('tg_language','en');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class AgentAvatarBrowserTest(unittest.TestCase):
    def test_client_marks_and_identity(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
