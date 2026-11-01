"""Create a fresh namesake after removal, without implicitly reattaching the old identity."""
import unittest
import test_direct_browser as browser

PROBE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<300;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
 const join=async code=>{
  const r=await fetch('/api/join',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});
  const data=await r.json();if(!r.ok)throw Error(data.error);return data;
 };
 const agent=async(token,path,data)=>{
  const r=await fetch(path,{method:data?'POST':'GET',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json','X-Telegent-Project':'main'},body:data?JSON.stringify(data):undefined});
  return {status:r.status,...await r.json()};
 };
 try{
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  const name='reuse-'+VIEWPORT+'-'+Date.now();
  const old=await join((await api('POST','/api/invites',{name,kind:'agent'})).code);
  const history=(await agent(old.token,'/api/messages',{body:'Old stable author fixture'})).id;
  const dm=(await api('POST','/api/conversations',{peer_id:old.id})).conversation.id;
  const privateId=(await api('POST','/api/messages',{body:'Old private fixture',conversation_id:dm})).id;
  await api('POST','/api/members/remove',{account_id:old.id});await loadPresence();
  openInvite(name);chooseInviteMode('agent');await loadCandidates();invNameChanged();
  check(CANDS.some(c=>c.id===old.id),'removed agent remains available for an explicit return');
  check(typedCand()===null&&$('invCreate').textContent===tr('Создать ссылку'),'new-agent mode creates an invitation instead of silently returning an old account');
  $('invCreate').click();await wait(()=>INV_TRACK?.name===name&&INV_TRACK?.purpose==='new');
  check(!ME.users.some(u=>u.id===old.id),'creating a link does not reattach the old identity');
  const code=$('invLink').value.match(/tgi_[a-f0-9]+/)[0],fresh=await join(code);
  check(fresh.id!==old.id&&fresh.token!==old.token,'replacement gets a separate stable id and token');
  check((await agent(fresh.token,'/api/messages/'+privateId)).status===404,'replacement cannot inherit an old private conversation');
  check((await agent(old.token,'/api/messages')).status===404,'removed token cannot access project messages');
  $('invClose').click();await loadPresence();await refresh();
  check(MSGS.get(history).author_id===old.id&&ME.users.some(u=>u.id===fresh.id),'history keeps the previous author and new membership stays separate');
  await api('POST','/api/members/remove',{account_id:fresh.id});await loadPresence();
  openInvite('');chooseInviteMode('existing');await loadCandidates();
  const button=$('invCands').querySelector('[data-act="madd"][data-account="'+old.id+'"]');
  check(!!button,'existing-account mode offers the precise old identity');button.click();
  await wait(()=>ME.users.some(u=>u.id===old.id));
  check(!ME.users.some(u=>u.id===fresh.id),'explicit return keeps namesakes distinct');
  $('invClose').click();localStorage.setItem('tg_language','en');
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,checks,viewport:VIEWPORT})});
 }catch(error){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:error.message,stack:error.stack,checks,viewport:VIEWPORT})})}
})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class AgentRecreationBrowserTest(unittest.TestCase):
    def test_fresh_agent_and_explicit_existing_identity(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,PROBE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
