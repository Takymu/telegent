"""Real browser workflows, explicit receipts, onboarding and RU/EN layouts."""
import unittest
import test_direct_browser as browser

PROBE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
 const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
 const wait=async fn=>{for(let i=0;i<200;i++){if(fn())return;await pause(30)}throw Error('render timeout')};
 const actor=async(method,path,data)=>{
  const r=await fetch(path,{method,headers:{Authorization:'Bearer '+FIXTURE.agent_token,'Content-Type':'application/json','X-Telegent-Project':'main'},body:data?JSON.stringify(data):undefined});
  const body=await r.json();if(!r.ok)throw Error(body.error);return body;
 };
 try {
  await wait(()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general));
  if(UI_LANG==='en') {
    check(document.documentElement.lang==='en','English document language');
    check($('cBody').value==='Черновик Общий чат Сохранить','language change preserves draft verbatim');
    const fixture=JSON.parse(localStorage.getItem('ux-fixture-'+VIEWPORT));
    check(MSGS.get(fixture.mid)?.body==='Общий чат Сохранить '+VIEWPORT,'user message is never translated');
    check(PROJECTS.find(p=>p.slug===FIXTURE.second)?.title==='Сохранить','project title is never translated');
    check(document.querySelector('.tabs [data-tab="feed"]').textContent.startsWith('Team chat'),'English navigation');
    showSettings();check($('infoHeading').textContent==='Settings','English settings');
    check($('infoBody').querySelector('[data-ux="secretaries"]').textContent==='Secretaries','secretary settings translated');
    check($('infoBody').querySelector('[data-color-scheme]').value==='telegram','color scheme survives reload');
    check($('infoBody').querySelector('[data-color-scheme]').textContent.includes('Gray (white ticks)'),'color choices translated');
    check($('infoBody').querySelector('[data-language]').value==='en','manual language override retained');closeInfo();
    await showReceipt(fixture.mid);check($('infoHeading').textContent==='Message information','English message info');
    check($('infoBody').textContent.includes('Общий чат Сохранить '+VIEWPORT),'English info preserves original message');closeInfo();
    openInvite('');chooseInviteMode('agent');
    check($('invModeLabel').textContent==='Connect an agent','English invitation wizard');$('invClose').click();
    await selectTab('more');check(!$('moreView').classList.contains('hidden'),'English More section');
    await selectTab('work');await loadWork();check(!$('workView').classList.contains('hidden'),'English work screen');
    check(!/[А-Яа-яЁё]/.test(document.querySelector('#workView h2').textContent),'work heading translated');
    let error='';try{await api('POST','/api/project/rename',{title:''})}catch(e){error=e.message;}
    check(error&&!/[А-Яа-яЁё]/.test(error),'server validation follows selected language');
    check(document.documentElement.scrollWidth<=innerWidth,'English page has no horizontal overflow');
    await selectTab('feed');
    localStorage.setItem('tg_language','auto');
    await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});return;
  }
  check(UI_LANG==='ru','Russian initial UI');
  for(const id of ['pwInput','joinPw','joinPw2','joinLPw','pwOld','pwNew','pwNew2']){
    const input=$(id),button=input.parentElement.querySelector('.password-eye');input.value='fixture value';
    check(input.type==='password'&&button.type==='button','password starts hidden with a non-submit toggle');button.click();
    check(input.type==='text'&&input.value==='fixture value'&&button.getAttribute('aria-pressed')==='true','eye shows unchanged password');button.click();
    check(input.type==='password'&&input.value==='fixture value','eye hides unchanged password');input.value='';
  }
  if(VIEWPORT==='desktop'){
    const handle=$('projectsResize');
    check(getComputedStyle(handle).cursor==='col-resize','left border exposes resize cursor');
    handle.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
    check($('projects').getBoundingClientRect().width===240,'left panel resizes by keyboard');
    projectsWidth(10000,true);check($('projects').getBoundingClientRect().width<=500&&document.querySelector('main').getBoundingClientRect().width>=359,'left panel clamps and preserves chat space');
    handle.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));check($('projects').getBoundingClientRect().width===220,'left panel reset');
  }else check(getComputedStyle($('projectsResize')).display==='none','left resize border hidden on mobile');
  const originalTitle=$('projTitle').textContent;$('projTitle').textContent='Project development team';
  check($('projTitle').scrollWidth<=$('projTitle').clientWidth||VIEWPORT==='mobile'&&document.querySelector('#app header [style="flex:1"]').getBoundingClientRect().width<=1,'title uses free header space before ellipsis, including the attachments button');$('projTitle').textContent=originalTitle;
  const peer=ME.users.find(u=>u.name==='alice-agent');
  const originalApi=api;
  for(const body of ['Brief fixture','Wrapped fixture '.repeat(12),'**Bold fixture**\n\nSecond paragraph fixture']){
    let release;const gate=new Promise(resolve=>release=resolve);
    api=async(...args)=>{if(args[0]==='POST'&&args[1]==='/api/messages')await gate;return originalApi(...args)};
    $('cTo').value=peer.name;$('cBody').value=body;$('sendBtn').click();
    await wait(()=>!!$('pendingOutgoing').querySelector('.receipt-spinner'));
    const shape=node=>{const bubble=node.querySelector('.mmain'),b=bubble.getBoundingClientRect(),r=bubble.querySelector('.receipt-button').getBoundingClientRect();return [b.width,b.height,r.left+r.width/2-b.left,r.top+r.height/2-b.top]};
    const pendingShape=shape($('pendingOutgoing').querySelector('.mrow'));release();
    await wait(()=>!SENDING&&[...MSGS.values()].some(m=>m.body===body));
    const sent=[...MSGS.values()].find(m=>m.body===body),sentShape=shape($('m'+sent.id));
    check(pendingShape.every((value,i)=>Math.abs(value-sentShape[i])<1),'pending and delivered bubbles keep the same size and receipt position');
  }
  api=originalApi;$('cTo').value='';
  const root=(await api('POST','/api/messages',{body:'Original root fixture'})).id;
  const answer=(await actor('POST','/api/messages',{body:'Answer first line\nAnswer second line',reply_to:root})).id;
  const nested=(await api('POST','/api/messages',{body:'Reply to answer',reply_to:answer})).id;
  await refresh();MSGS.delete(answer);render(true);
  const quote=$('m'+nested).querySelector('.mquote');
  check(+quote.dataset.id===answer&&quote.textContent.includes('Answer first line Answer second line')&&!quote.textContent.includes('Original root fixture'),'nested quote uses exact parent body even outside loaded page');
  check(quote.querySelector('.quote-author').textContent==='alice-agent','quote author is separate from body');
  const quotedParent=(await actor('POST','/api/messages',{body:'Long quote fixture '.repeat(25)})).id;
  const quotedReply=(await api('POST','/api/messages',{body:'Short answer',reply_to:quotedParent})).id;
  await refresh();
  check($('m'+quotedReply).querySelector('.mmain').getBoundingClientRect().width<=300,'long quote does not stretch a short reply');
  const threadButton=$('m'+root).querySelector('.message-footer [data-act="thread"]');
  check(Math.abs(threadButton.getBoundingClientRect().left-$('m'+root).querySelector('.mtext').getBoundingClientRect().left)<2,'reply count aligns with left text edge');
  showSettings();const colors=$('infoBody').querySelector('[data-color-scheme]');
  colors.value='default';colors.dispatchEvent(new Event('change',{bubbles:true}));
  check(document.documentElement.dataset.colors==='default','default palette can be restored');
  colors.value='telegram';colors.dispatchEvent(new Event('change',{bubbles:true}));closeInfo();
  const compact=(await api('POST','/api/messages',{body:'Short fixture'})).id;await refresh();
  const ownRow=$('m'+compact),own=ownRow.querySelector('.mmain'),incoming=$('m'+FIXTURE.agent).querySelector('.mmain');
  check(ownRow.classList.contains('outgoing')&&incoming.closest('.mrow').classList.contains('incoming'),'alignment follows authenticated author identity');
  if(VIEWPORT==='desktop')for(const bubble of [own,incoming]){
    const tools=bubble.querySelector('.mtools');tools.style.display='flex';
    check(Math.abs(tools.getBoundingClientRect().right-bubble.getBoundingClientRect().right+10)<2,'hover actions are anchored to their bubble');
    tools.style.display='';
  }
  check(own.getBoundingClientRect().right>incoming.getBoundingClientRect().right,'own bubble is to the right of incoming');
  check(own.getBoundingClientRect().width<$('feed').getBoundingClientRect().width*.8,'short bubble fits its content');
  check(getComputedStyle(own).borderTopWidth==='0px'&&getComputedStyle(incoming).borderTopWidth==='0px','message fills have no outlines');
  check(getComputedStyle(own).backgroundColor!==getComputedStyle(incoming).backgroundColor,'own and incoming fills differ');
  check(getComputedStyle(own).backgroundColor==='rgb(96, 96, 96)','gray scheme applies to own bubble');
  check(getComputedStyle(own.querySelector('[data-act="receipt"]')).color==='rgb(255, 255, 255)','gray scheme uses white ticks');
  const range=document.createRange();range.selectNodeContents(own.querySelector('.mtext p').firstChild);
  check(Math.abs(centerY(range.getBoundingClientRect())-centerY(own.querySelector('.message-meta').getBoundingClientRect()))<8,'short text and timestamp share final line');
  check(!own.querySelector('.mhead .mtime')&&!own.querySelector('.mtext button,.mtext time'),'time and controls stay outside message source');
  function centerY(r){return r.top+r.height/2}
  const mid=(await api('POST','/api/messages',{body:'Общий чат Сохранить '+VIEWPORT})).id;
  await actor('GET','/api/messages/'+mid);
  await actor('POST','/api/messages/'+mid+'/reaction',{emoji:'🔥',active:true});await refresh();
  check(!!document.querySelector('#m'+mid+' .receipt-button svg'),'compact receipt icon is visible');
  check(!document.querySelector('#m'+mid+' .mrc'),'recipient names removed from the feed');
  const footer=document.querySelector('#m'+mid+' .message-footer'),tick=footer.querySelector('[data-act="receipt"]');
  const center=node=>{const r=node.getBoundingClientRect();return r.top+r.height/2};
  check(Math.abs(center(tick)-center(footer.querySelector('.reaction')))<1,'one reaction shares a line with receipt');
  check(Math.abs(tick.querySelector('svg').getBoundingClientRect().width-17.5)<.1,'receipt icon reduced by factor 1.2');
  const unread={...MSGS.get(mid),receipts:[{user:'alice-agent',delivered:1,read:null}]};
  const probe=document.createElement('div');probe.innerHTML=receiptButton(unread);
  check(probe.querySelectorAll('path').length===1,'delivered but unread gives one tick');
  probe.innerHTML=receiptButton({...unread,receipts:[{user:'alice-agent',delivered:1,read:1}]});
  check(probe.querySelectorAll('path').length===2,'a read recipient gives two ticks');
  for(const emoji of ['❤️','👍','👎','🤡','😁','😭'])await actor('POST','/api/messages/'+mid+'/reaction',{emoji,active:true});
  await refresh();
  const crowded=document.querySelector('#m'+mid+' .message-footer');crowded.style.width='120px';
  check(center(crowded.querySelector('[data-act="receipt"]'))>center(crowded.querySelector('.reaction')),'crowded reactions wrap receipt');
  check(crowded.scrollWidth<=crowded.clientWidth,'wrapped reactions stay within footer');crowded.style.width='';
  document.querySelector('#m'+mid+' [data-act="more"]').click();
  for(const emoji of ['😁','😭'])check(!!$('menu').querySelector('[data-act="react"][data-emoji="'+emoji+'"]'),'new reaction is selectable');
  check($('menu').scrollWidth<=$('menu').clientWidth,'reaction picker fits mobile menu');
  check($('menu').querySelector('.menu-message').textContent.includes('Общий чат Сохранить'),'tap menu keeps original message visible');
  check($('menu').querySelector('.menu-details').textContent.includes('alice-agent')&&$('menu').querySelector('.menu-details').textContent.includes('🔥'),'readers and reactions share message menu');closeMenu();
  await showReceipt(mid);check($('infoBody').textContent.includes('alice-agent')&&$('infoBody').textContent.includes('🔥'),'message info shows readers and reactions');closeInfo();
  const reply=(await actor('POST','/api/messages',{body:'thread answer fixture',reply_to:mid})).id;
  $('q').value='Общий чат Сохранить '+VIEWPORT;updateFilterCount();await reload();
  $('filters').classList.add('open');$('closeFilters').click();
  check(!$('filters').classList.contains('open')&&$('filterSummary').textContent.includes('Общий чат'),'active search remains visible after closing');
  document.querySelector('#m'+mid+' [data-act="thread"]').click();await wait(()=>THREAD===mid&&MSGS.has(reply));
  check(MSGS.get(mid).reply_count===1,'thread count excludes service records');
  document.querySelector('#threadBar [data-act="unthread"]').click();await wait(()=>!THREAD&&$('q').value.includes(VIEWPORT));
  check(MSGS.has(mid),'thread return restores filtered source');
  document.querySelector('#filterSummary [data-ux="reset-filters"]').click();await wait(()=>!hasFilters()&&MSGS.has(FIXTURE.general));
  const question=(await actor('POST','/api/messages',{type:'question',body:'question fixture '+VIEWPORT,to_ids:[ME.id]})).id;
  WORK_MODE='waiting';await selectTab('work');await loadWork();
  check(!!document.querySelector('#workBox [data-id="'+question+'"]'),'incoming question is actionable');
  document.querySelector('#workBox [data-ux="waiting-dismiss"][data-id="'+question+'"]').click();
  await wait(()=>!WAITING_SAVING&&!document.querySelector('#workBox [data-reminder="'+question+'"]'));
  await loadWork();check(!document.querySelector('#workBox [data-reminder="'+question+'"]'),'dismissal survives reload');
  check((await api('GET','/api/messages/'+question)).message.body.includes('question fixture'),'dismissal preserves message');
  const approval=(await actor('POST','/api/messages',{body:'approval fixture '+VIEWPORT,needs_approval:true,approver:ME.name})).id;
  WORK_MODE='approval';await loadWork();await openAction(approval,'approval','approve');
  $('actionNote').value='fixture decision';$('actionSave').click();await wait(()=>!$('actionDialog').open&&!$('actionSave').disabled);
  check((await api('GET','/api/messages/'+approval)).message.approvals.some(a=>a.by_id===ME.id),'approval form persists human decision');
  await actor('POST','/api/board',{now:'Declared fixture task',next:'Fixture next step'});
  await actor('POST','/api/activity',{state:'working',client:'f'.repeat(32)});await loadPresence();
  const participant=document.querySelector('.participant-card[data-account="'+peer.id+'"]');
  check(participant.textContent.includes('работает')&&!participant.textContent.includes('Declared fixture task'),'participant list shows compact runtime activity');
  await actor('POST','/api/activity',{state:'idle',client:'f'.repeat(32)});await loadPresence();
  check(!document.querySelector('.participant-card[data-account="'+peer.id+'"]').textContent.includes('работает'),'idle client does not appear working');
  await selectTab('board');await loadBoard();check($('boardBox').textContent.includes('Declared fixture task'),'board shows explicit reported work');
  await showMember(peer.id);check($('infoBody').textContent.includes('Declared fixture task')&&$('infoBody').textContent.includes('Наличие связи'),'participant card separates connection and declared work');closeInfo();
  await selectTab('feed');openInvite('');
  check(!$('invChoices').classList.contains('hidden')&&$('invFields').classList.contains('hidden'),'invite starts with a simple choice');
  chooseInviteMode('agent');$('invName').value='new-'+VIEWPORT;invNameChanged();$('invCreate').click();
  await wait(()=>!$('invResult').classList.contains('hidden'));
  check($('invProgress').textContent.includes('Ожидаем'),'invitation does not claim connection prematurely');
  const code=$('invLink').value.match(/#invite=([^"\s]+)/)[1];
  const join=await fetch('/api/join',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});
  const joined=await join.json();check(join.ok,'agent accepts actual invitation');
  await loadPresence();check($('invProgress').textContent.includes('Ожидаем подключения'),'accepted invitation differs from listening');
  await fetch('/api/summary?project=main&client=codex',{headers:{Authorization:'Bearer '+joined.token}});await loadPresence();
  check($('invProgress').textContent.includes('проверяет входящие'),'actual client check updates onboarding stage');$('invClose').click();
  openInvite('');chooseInviteMode('secretary');$('invName').value='assistant-'+VIEWPORT;invNameChanged();
  check($('invNew').style.display==='none'&&$('invNote').textContent.includes('будущих проектах'),'secretary invitation explains personal scope');
  $('invCreate').click();await wait(()=>!$('invResult').classList.contains('hidden'));
  check($('invLink').value.includes('--project <slug>'),'copied secretary instruction explains explicit project selection');
  const secretaryCode=$('invLink').value.match(/#invite=([^"\s]+)/)[1];
  const secretaryResponse=await fetch('/api/join',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:secretaryCode})});
  const secretary=await secretaryResponse.json();check(secretaryResponse.ok&&secretary.is_secretary&&!secretary.project,'secretary joins without a pinned project');
  $('invClose').click();await loadPresence();
  check(document.querySelector('.participant-card[data-account="'+secretary.id+'"]').textContent.includes('секретарь'),'participant role identifies secretary');
  await showMember(secretary.id);check($('infoBody').textContent.includes('Секретарь · человек-владелец:'),'participant card identifies secretary');closeInfo();
  const secondMembership=await fetch('/api/me?project='+FIXTURE.second,{headers:{Authorization:'Bearer '+secretary.token}});
  check((await secondMembership.json()).project?.slug===FIXTURE.second,'secretary joins another current project automatically');
  showSettings();$('infoBody').querySelector('[data-ux="secretaries"]').click();await wait(()=>$('infoBody').querySelector('[data-ux="secretary-revoke-confirm"]'));
  $('infoBody').querySelector('[data-ux="secretary-revoke-confirm"]').click();
  check($('infoBody').textContent.includes('Сообщения останутся'),'disconnect explains retained history');
  $('infoBody').querySelector('[data-ux="secretary-revoke"]').click();await wait(()=>$('infoHeading').textContent==='Секретари'&&!$('infoBody').querySelector('[data-ux="secretary-revoke-confirm"]'));closeInfo();
  const png=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO8kAAAAASUVORK5CYII='),c=>c.charCodeAt(0));
  await addFiles([new File([png],'fixture.png',{type:'image/png'})]);
  check(!!$('cPending').querySelector('.file-thumb'),'pending image has thumbnail');
  $('cBody').value='image fixture '+VIEWPORT;$('sendBtn').click();
  await wait(()=>!SENDING&&[...MSGS.values()].some(m=>m.body==='image fixture '+VIEWPORT));
  const image=[...MSGS.values()].find(m=>m.body==='image fixture '+VIEWPORT);
  const imageRow=$('m'+image.id);
  await wait(()=>imageRow.querySelector('.inline-image img')?.naturalWidth===1);
  check(!imageRow.querySelector('.file-card')&&!imageRow.textContent.includes('Посмотреть изображение'),'image loads inline without a separate preview button');
  check(imageRow.querySelector('.inline-image').getBoundingClientRect().right<=imageRow.querySelector('.mmain').getBoundingClientRect().right,'image fits message outline');
  const cached=INLINE_IMAGES.get(String(image.attachments[0].id));render(true);await pause(30);
  check(INLINE_IMAGES.get(String(image.attachments[0].id))===cached,'live render reuses authenticated image');
  $('m'+image.id).querySelector('.inline-image').click();await wait(()=>$('infoBody').querySelector('img')?.complete);
  check(!!$('infoBody').querySelector('[data-act="dl"]'),'image viewer has explicit download');
  check($('infoBody').querySelector('img').naturalWidth===1,'authenticated image preview decodes');closeInfo();await pause(20);check(!PREVIEW_URL,'closing preview releases blob URL');
  await actor('POST','/api/claims',{key:'fixture-'+VIEWPORT,hours:1});await selectTab('claims');await loadClaims();
  check($('claimsTable').querySelector('td[data-label]'),'mobile claims expose readable field labels');
  await selectTab('feed');await pause(50);await selectTab('direct');history.back();await wait(()=>TAB==='feed');
  check(!ACTIVE_DM,'browser Back returns to source section');
  await api('POST','/api/project/rename?project='+FIXTURE.second,{title:'Сохранить'});await loadProjects();
  $('cBody').value='Черновик Общий чат Сохранить';$('cBody').dispatchEvent(new Event('input'));saveDraft();
  localStorage.setItem('ux-fixture-'+VIEWPORT,JSON.stringify({mid}));
  showSettings();const language=$('infoBody').querySelector('[data-language]');language.value='en';
  await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT,checks})});
  language.dispatchEvent(new Event('change',{bubbles:true}));
 }catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message,stack:e.stack,viewport:VIEWPORT,checks})})}
})();
'''

RESTORE=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
(async()=>{const pause=ms=>new Promise(r=>setTimeout(r,ms));try{
 for(let i=0;i<200&&!(typeof ME!=='undefined'&&ME?.project&&COMPOSE_SCOPE===draftKey());i++)await pause(30);
 if(UI_LANG!=='ru'||LANGUAGE_SETTING!=='auto')throw Error('automatic language does not follow Russian device locale');
 if($('cBody').value!=='Черновик Общий чат Сохранить')throw Error('draft lost across language and browser restart');
 if(document.documentElement.scrollWidth>innerWidth)throw Error('Russian layout overflow');
 await fetch('/debug/result',{method:'POST',body:JSON.stringify({ok:true,viewport:VIEWPORT})});
}catch(e){await fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})})}})();
'''


@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class UXBrowserTest(unittest.TestCase):
    def test_workflows_language_switch_and_mobile_layout(self):
        old=(browser.PROBE,browser.RESTORE_PROBE,browser.BROWSER_LOCALE,browser.EXTRA_PROBE)
        browser.PROBE,browser.RESTORE_PROBE,browser.BROWSER_LOCALE,browser.EXTRA_PROBE=PROBE,PROBE,'ru-RU',RESTORE
        try:
            browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:
            browser.PROBE,browser.RESTORE_PROBE,browser.BROWSER_LOCALE,browser.EXTRA_PROBE=old


if __name__=='__main__':unittest.main(verbosity=2)
