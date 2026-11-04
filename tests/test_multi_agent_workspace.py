"""Separate accounts, receipts and hook delivery for sessions sharing one folder."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

import test_attachments as fixture


class MultiAgentWorkspaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AttachmentTest.setUpClass(); cls.fx=fixture.AttachmentTest()

    @classmethod
    def tearDownClass(cls): fixture.AttachmentTest.tearDownClass()

    def setUp(self):
        self.root=self.fx.work/uuid.uuid4().hex;self.root.mkdir()
        self.env={k:v for k,v in self.fx.env.items() if not k.startswith(('CODEX_','CLAUDE','KIMI','TELEGENT_'))}
        self.env.update(HOME=str(self.root/'home'),USERPROFILE=str(self.root/'home'))

    def cli(self,*args,env=None,stdin=None):
        return subprocess.run([sys.executable,'-X','utf8',str(fixture.ROOT/'tg.py'),*args],
            cwd=self.root,env=env or self.env,input=stdin,capture_output=True,text=True,encoding='utf-8',timeout=30)

    def connect(self,client,who='alice',session_env=True):
        sid=str(uuid.uuid4());name='agent-'+uuid.uuid4().hex[:10]
        invite=self.fx.api('POST','/api/invites',{'name':name,'kind':'agent'},who=who)
        env=dict(self.env)
        if session_env:
            if client=='codex':env['CODEX_THREAD_ID']=sid
            elif client=='claude':env.update(CLAUDECODE='1',CLAUDE_CODE_SESSION_ID=sid)
            else:env['KIMI_SESSION_ID']=sid
        result=self.cli('connect',self.fx.url+'/#invite='+invite['code'],'--client',client,'--url-source','',env=env)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        registry=json.loads((self.root/'.telegent/connections.json').read_text())
        relative=(registry['sessions'][client+':'+sid] if session_env else
                  next(row['config'] for row in registry['connections'] if row['name']==name))
        self.assertIn(str(self.root/relative),result.stdout)
        config=json.loads((self.root/relative).read_text())
        self.assertEqual(config['url_source'],'')
        config['auto_update']=False
        (self.root/relative).write_text(json.dumps(config,indent=2),encoding='utf-8')
        self.fx.tokens[config['account_id']]=Path(config['token_file']).read_text().strip()
        return dict(name=name,id=config['account_id'],env=env,sid=sid,client=client,config=config,path=relative,invite=invite)

    def assert_isolation(self,client_a,client_b):
        first=self.connect(client_a)
        legacy=(self.root/'.telegent.json').read_bytes()
        token=Path(first['config']['token_file']).read_bytes()
        second=self.connect(client_b)
        self.assertEqual((self.root/'.telegent.json').read_bytes(),legacy)
        self.assertEqual(Path(first['config']['token_file']).read_bytes(),token)
        self.assertNotEqual(first['config']['token_file'],second['config']['token_file'])
        self.assertNotEqual(first['config']['state_file'],second['config']['state_file'])
        for actor in (first,second):
            result=self.cli('whoami',env=actor['env'])
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn(actor['name'],result.stdout)
            result=self.cli('send','Identity fixture '+actor['name'],env=actor['env'])
            self.assertEqual(result.returncode,0,result.stderr)
            rows=self.fx.api('GET','/api/messages?limit=1000',who='alice')['messages']
            self.assertEqual(rows[-1]['author_id'],actor['id'])
            self.assertEqual(self.cli('ack','--all',env=actor['env']).returncode,0)
        for actor in (first,second):
            actor['message']=self.fx.api('POST','/api/messages',{'body':'Private routing fixture',
                'type':'question','to_ids':[actor['id']]},who='alice')['id']
        for actor,other in ((first,second),(second,first)):
            result=self.cli('inbox',env=actor['env'])
            self.assertIn('#'+str(actor['message'])+' ',result.stdout)
            self.assertNotIn('#'+str(other['message'])+' ',result.stdout)
            event=json.dumps({'hook_event_name':'SessionStart','session_id':actor['sid'],'cwd':str(self.root)})
            command=['codex','hook'] if actor['client']=='codex' else ['hook','--session-start']
            if actor['client'] in ('codex','claude'):
                result=self.cli(*command,'--workspace',str(self.root),stdin=event)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn('#'+str(actor['message']),result.stdout)
                self.assertNotIn('#'+str(other['message'])+' ',result.stdout)
        self.cli('ack',str(second['message']),env=second['env'])
        self.assertEqual(self.cli('inbox','--has-new',env=first['env']).stdout.strip(),'1')
        result=self.cli('connect',self.fx.url+'/#invite='+first['invite']['code'],'--client',first['client'],'--url-source','',env=first['env'])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(Path(first['config']['token_file']).read_bytes(),token)
        return first,second

    def test_two_codex_sessions(self):self.assert_isolation('codex','codex')
    def test_two_claude_sessions(self):self.assert_isolation('claude','claude')
    def test_codex_and_claude(self):self.assert_isolation('codex','claude')
    def test_two_kimi_sessions(self):self.assert_isolation('kimi','kimi')

    def test_unbound_session_does_not_guess_an_identity(self):
        first=self.connect('codex');self.connect('codex')
        env=dict(self.env,CODEX_THREAD_ID=str(uuid.uuid4()))
        result=self.cli('send','must not be sent',env=env)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('--agent',result.stderr)
        result=self.cli('connections',env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        event=json.dumps({'hook_event_name':'SessionStart','session_id':env['CODEX_THREAD_ID']})
        result=self.cli('codex','hook','--workspace',str(self.root),stdin=event,env=env)
        self.assertEqual(json.loads(result.stdout),{})
        result=self.cli('--config',str(self.root/first['path']),'codex','hook','--workspace',str(self.root),stdin=event,env=env)
        self.assertEqual(json.loads(result.stdout),{})
        result=self.cli('--agent',first['id'],'whoami',env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(first['name'],result.stdout)

    def test_projects_and_private_chats_remain_separate(self):
        first=self.connect('codex');second=self.connect('codex',who='carol')
        private=self.fx.api('POST','/api/conversations',{'peer_id':first['id']},who='alice')['conversation']
        message=self.fx.api('POST','/api/messages',{'body':'Private fixture',
            'conversation_id':private['id']},who='alice')['id']
        self.assertEqual(self.cli('read',str(message),env=first['env']).returncode,0)
        denied=self.cli('read',str(message),env=second['env'])
        self.assertEqual(denied.returncode,2,denied.stdout+denied.stderr)
        self.assertIn('404',denied.stderr)
        result=self.cli('send','Other project fixture',env=second['env'])
        self.assertEqual(result.returncode,0,result.stderr)
        rows=self.fx.api('GET','/api/messages?limit=1000',who='carol')['messages']
        self.assertEqual(rows[-1]['author_id'],second['id'])

    def test_existing_verified_listener_binding_is_preserved(self):
        first=self.connect('codex')
        state=Path(first['config']['state_file']+'.receiver');state.mkdir(parents=True,exist_ok=True)
        (state/'binding.json').write_text(json.dumps({'account_id':first['id'],
            'cwd':str(self.root),'thread':first['sid']}))
        (self.root/'.telegent/connections.json').unlink()  # emulate the pre-registry client
        second=self.connect('codex')
        registry=json.loads((self.root/'.telegent/connections.json').read_text())
        self.assertEqual(registry['sessions']['codex:'+first['sid']],first['path'])
        self.assertEqual(registry['sessions']['codex:'+second['sid']],second['path'])
        self.assertEqual(self.cli('whoami',env=first['env']).returncode,0)

    def test_kimi_without_a_native_session_env_can_use_the_printed_config(self):
        first=self.connect('kimi',session_env=False);second=self.connect('kimi',session_env=False)
        for actor in (first,second):
            result=self.cli('--config',str(self.root/actor['path']),'whoami')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn(actor['name'],result.stdout)

    def test_registry_cannot_point_to_a_config_outside_the_workspace(self):
        actor=self.connect('codex')
        registry=json.loads((self.root/'.telegent/connections.json').read_text())
        registry['connections'][0]['config']='../client.json'
        (self.root/'.telegent/connections.json').write_text(json.dumps(registry))
        result=self.cli('whoami',env=actor['env'])
        self.assertNotEqual(result.returncode,0)
        self.assertIn('реестр',result.stderr)

    def test_generated_hook_routes_by_event_session_instead_of_the_installer(self):
        for client in ('codex','claude'):
            first=self.connect(client);second=self.connect(client)
            for actor in (first,second):
                self.cli('ack','--all',env=actor['env'])
                actor['message']=self.fx.api('POST','/api/messages',{'body':'Hook fixture',
                    'type':'question','to_ids':[actor['id']]},who='alice')['id']
            settings=json.loads((self.root/('.codex/hooks.json' if client=='codex' else '.claude/settings.local.json')).read_text())
            handler=settings['hooks']['SessionStart'][-1]['hooks'][0]
            for actor,other in ((first,second),(second,first)):
                if client=='claude':command=[handler['command'],*handler['args']]
                else:command=(['powershell.exe','-NoProfile','-NonInteractive','-Command',handler['command']]
                              if os.name=='nt' else handler['command'])
                result=subprocess.run(command,shell=client=='codex' and os.name!='nt',cwd=self.root,env=self.env,
                    input=json.dumps({'session_id':actor['sid'],'hook_event_name':'SessionStart','cwd':str(self.root)}),
                    capture_output=True,text=True,encoding='utf-8',timeout=30)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn('#'+str(actor['message']),result.stdout)
                self.assertNotIn('#'+str(other['message'])+' ',result.stdout)

    def test_connections_are_serialized_without_overwriting_the_first_account(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            actors=list(pool.map(self.connect,['codex','codex']))
        registry=json.loads((self.root/'.telegent/connections.json').read_text())
        self.assertEqual(len(registry['connections']),2)
        for actor in actors:
            result=self.cli('whoami',env=actor['env'])
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn(actor['name'],result.stdout)

    def test_nested_workspace_connections_are_excluded_from_git(self):
        parent=self.root
        subprocess.run(['git','init','--quiet',str(parent)],check=True,capture_output=True)
        self.root=parent/'work';self.root.mkdir()
        self.connect('codex');self.connect('codex')
        paths=['work/.telegent.json','work/.telegent/connections.json','work/.codex/hooks.json']
        result=subprocess.run(['git','check-ignore',*paths],cwd=parent,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(set(result.stdout.splitlines()),set(paths))

    def test_token_reset_updates_its_session_without_touching_the_other_agent(self):
        first=self.connect('codex');second=self.connect('codex')
        original=(self.root/first['path']).read_bytes()
        old_token=Path(second['config']['token_file']).read_text()
        invite=self.fx.api('POST','/api/reset',{'account_id':second['id']},who='alice')
        result=self.cli('connect',self.fx.url+'/#invite='+invite['code'],'--client','codex','--force',
                        '--url-source','',env=second['env'])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/first['path']).read_bytes(),original)
        registry=json.loads((self.root/'.telegent/connections.json').read_text())
        self.assertEqual(len(registry['connections']),2)
        saved=json.loads((self.root/second['path']).read_text())
        self.assertEqual(saved['state_file'],second['config']['state_file'])
        self.assertNotEqual(Path(saved['token_file']).read_text(),old_token)
        for actor in (first,second):
            result=self.cli('whoami',env=actor['env'])
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn(actor['name'],result.stdout)

    def test_validated_listener_can_register_a_session_from_a_subdirectory(self):
        first=self.connect('codex');self.connect('codex')
        spec=fixture.importlib.util.spec_from_file_location('workspace_client',fixture.ROOT/'tg.py')
        client=fixture.importlib.util.module_from_spec(spec);spec.loader.exec_module(client)
        child=self.root/'src';child.mkdir();sid=str(uuid.uuid4())
        client.bind_workspace_connection(client.Conf(str(self.root/first['path'])),child,'codex',sid)
        result=self.cli('whoami',env=dict(self.env,CODEX_THREAD_ID=sid))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(first['name'],result.stdout)


if __name__=='__main__':unittest.main(verbosity=2)
