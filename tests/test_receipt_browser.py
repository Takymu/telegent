"""Addressed and broadcast ticks with real receipts and an unread chat observer."""
import unittest

import test_direct_browser as browser


PROBE = r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<180;i++){if(fn())return;await pause(30)}throw Error('render timeout')};
 const as=async(token,method,path,data)=>{
   const response=await fetch(path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json','X-Telegent-Project':'main'},body:data?JSON.stringify(data):undefined});
   const result=await response.json();if(!response.ok)throw Error(result.error);return result;
 };
 try{
   await wait(()=>typeof ME!=='undefined'&&ME?.project&&MSGS.has(FIXTURE.general));
   const name='observer-'+Math.random().toString(36).slice(2,14);
   const invite=await api('POST','/api/invites',{name,kind:'human'});
   const response=await fetch('/api/join',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:invite.code,password:'observer fixture password'})});
   const observer=await response.json();if(!response.ok)throw Error(observer.error);
   const mid=(await api('POST','/api/messages',{body:'addressed receipt fixture '+VIEWPORT,to_ids:['alice-agent']})).id;
   await as(observer.token,'GET','/api/messages?after='+(mid-1));
   await refresh();
   const ticks=id=>$('m'+id).querySelectorAll('.receipt-button svg path').length;
   check(ticks(mid)===1,'unread addressee has one tick');
   await as(FIXTURE.agent_token,'POST','/api/receipts',{kind:'ack',ids:[mid]});
   await refresh();
   const receipts=MSGS.get(mid).receipts,watched=receipts.find(r=>r.user_id===observer.id);
   check(watched&&watched.is_recipient===false&&!watched.read,'open observer page remains unread and is not an addressee');
   check(receipts.find(r=>r.user_id==='alice-agent')?.acked,'agent acknowledgement persisted');
   check(ticks(mid)===2,'unread observer does not hide acknowledged addressee ticks');
   await showReceipt(mid);
   check($('infoBody').textContent.includes(name),'observer receipt remains available in message details');closeInfo();
   const broadcast=(await api('POST','/api/messages',{body:'broadcast receipt fixture '+VIEWPORT})).id;
   await as(observer.token,'GET','/api/messages?after='+(broadcast-1));
   await as(FIXTURE.agent_token,'POST','/api/receipts',{kind:'ack',ids:[broadcast]});
   await refresh();
   check(MSGS.get(broadcast).receipts.filter(r=>r.is_recipient).length===2,'broadcast addresses both agents');
   check(ticks(broadcast)===1,'broadcast still waits for its second addressed agent');
   await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
})();
'''


@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class ReceiptBrowserTest(unittest.TestCase):
    def test_observer_does_not_block_ticks_on_desktop_and_mobile(self):
        original = browser.PROBE, browser.RESTORE_PROBE, browser.EXTRA_PROBE
        browser.PROBE, browser.RESTORE_PROBE, browser.EXTRA_PROBE = PROBE, PROBE, None
        try:
            browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:
            browser.PROBE, browser.RESTORE_PROBE, browser.EXTRA_PROBE = original


if __name__ == '__main__':
    unittest.main(verbosity=2)
