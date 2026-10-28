"""Personal archive in real RU/EN desktop/mobile UI, including list reorder and reload."""
import unittest
import test_direct_browser as browser

COMMON=r'''
window.addEventListener('error',e=>fetch('/debug/result',{method:'POST',body:JSON.stringify({error:e.message})}));
const pause=ms=>new Promise(r=>setTimeout(r,ms));let checks=0;
const check=(ok,label)=>{if(!ok)throw Error(label);checks++};
const wait=async fn=>{for(let i=0;i<350;i++){if(fn())return;await pause(20)}throw Error('render timeout')};
const ready=()=>typeof ME!=='undefined'&&ME?.project&&NAV_READY&&MSGS.has(FIXTURE.general);
const project=slug=>PROJECTS.find(p=>p.slug===slug);
const menu=slug=>openProjectMenu(slug,projectMenuButton(slug));
const result=async error=>fetch('/debug/result',{method:'POST',body:JSON.stringify(error?{error:error.message,stack:error.stack,checks,viewport:VIEWPORT}:{ok:true,checks,viewport:VIEWPORT})});
'''
PROBE=COMMON+r'''
(async()=>{try{
 await wait(ready);
 for(const p of PROJECTS.filter(p=>p.sidebar_archived))await api('POST','/api/project/archive?project='+p.slug,{archived:false});
 await loadProjects();
 if(!PROJECTS.some(p=>p.slug==='drag-fixture'))await api('POST','/api/projects',{title:'Drag fixture',slug:'drag-fixture'});
 await loadProjects();if(innerWidth<600&&!$('projects').classList.contains('open'))mobilePanel('projects');
 const realRole=ME.project.role;ME.project.role='member';menu('main');
 check(!!$('menu').querySelector('[data-act="parchive"]'),'ordinary human member gets archive action');ME.project.role=realRole;
 $('menu').querySelector('[data-act="parchive"]').click();await wait(()=>project('main').sidebar_archived);
 check(PROJECT==='main'&&MSGS.has(FIXTURE.general),'archiving does not discard current chat');
 check(!!$('archivedProjects').querySelector('[data-slug="main"]')&&!$('archivedProjects').hidden,'current archived project is accessible');
 $('projList').querySelector('[data-act="parchivefolder"]').click();check($('archivedProjects').hidden,'archive can be collapsed');
 $('projList').querySelector('[data-act="parchivefolder"]').click();menu('main');
 $('menu').querySelector('[data-act="parchive"]').click();await wait(()=>!project('main').sidebar_archived);
 check(!$('projList').querySelector('.archived'),'restore returns project to the main list');
 await api('POST','/api/project/archive?project=main',{archived:true});await loadProjects();
 check(project('main').sidebar_archived&&ME.project.sidebar_archived,'external device preference refreshes selected project');
 await api('POST','/api/messages',{body:'New message in archived fixture'});await loadProjects();
 check(project('main').sidebar_archived,'new messages do not unarchive a project');
 // The archive group must not shift the drop index of the visible active projects.
 const active=PROJECTS.filter(p=>!p.sidebar_archived),moving=active[0],last=active.at(-1),archiveIndex=PROJECTS.findIndex(p=>p.slug==='main');
 const row=document.querySelector(`.pitem[data-slug="${moving.slug}"]`),rect=row.getBoundingClientRect(),target=document.querySelector(`.pitem[data-slug="${last.slug}"]`).getBoundingClientRect();
 row.querySelector('a').dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0,clientX:rect.left+40,clientY:rect.top+rect.height/2}));
 await wait(()=>PROJECT_DRAG?.active);
 document.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,clientX:rect.left+40,clientY:target.bottom+2}));
 document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,button:0}));
 await wait(()=>!PROJECT_ORDER_SAVING&&PROJECTS.filter(p=>!p.sidebar_archived).at(-1).id===moving.id);
 check(PROJECTS.findIndex(p=>p.slug==='main')===archiveIndex&&project('main').sidebar_archived,'reorder preserves archive slots and membership');
 await pause(650);
 const normal=api,before=project(FIXTURE.second).sidebar_archived;
 api=async(method,path,...rest)=>{if(path.startsWith('/api/project/archive'))throw Error('Fixture network failure');return normal(method,path,...rest)};
 await archiveProject(FIXTURE.second);api=normal;
 check(project(FIXTURE.second).sidebar_archived===before,'failed archive keeps the original list state');
 for(const p of PROJECTS)await api('POST','/api/project/archive?project='+p.slug,{archived:true});
 await loadProjects();
 check(PROJECTS.every(p=>p.sidebar_archived)&&!!$('projList').querySelector('[data-act="parchivefolder"]'),'all archived projects remain reachable');
 check(PROJECT==='main'&&MSGS.has(FIXTURE.general),'archiving every project does not leave the project');
 localStorage.setItem('tg_language','en');await result();
}catch(error){await result(error)}})();
'''
RESTORE=COMMON+r'''
(async()=>{try{
 await wait(ready);
 check(PROJECTS.every(p=>p.sidebar_archived),'archive survives a browser restart');
 check(PROJECT==='main'&&!$('archivedProjects').hidden,'direct archived project opens after restart');
 const second=project(FIXTURE.second);await archiveProject(second.slug);
 check(!project(second.slug).sidebar_archived&&project('main').sidebar_archived,'restoring one project does not restore others');
 if(innerWidth<600&&!$('projects').classList.contains('open'))mobilePanel('projects');
 check(document.documentElement.scrollWidth<=innerWidth,'archive fits the phone');await result();
}catch(error){await result(error)}})();
'''

@unittest.skipUnless(browser.Path(browser.FIREFOX).is_file(),'Headless Firefox is required')
class ArchiveBrowserTest(unittest.TestCase):
    def test_archive_restore_reload_and_reorder(self):
        original=browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE
        browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=PROBE,RESTORE,None
        try:browser.DirectBrowserTest('test_navigation_drafts_secret_cards_and_mobile_layout').test_navigation_drafts_secret_cards_and_mobile_layout()
        finally:browser.PROBE,browser.RESTORE_PROBE,browser.EXTRA_PROBE=original

if __name__=='__main__':unittest.main(verbosity=2)
