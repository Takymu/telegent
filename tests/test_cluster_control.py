"""Durable authority under simultaneous claims, partitions, lost replies and restarts."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from cluster_control import ControlError, Coordinator, GENESIS, verify_entries


OPERATOR='operator-fixture-'+'o'*48
TOKENS={'node-a':'node-a-fixture-'+'a'*48,'node-b':'node-b-fixture-'+'b'*48}
INSTANCES={'node-a':'boot-a','node-b':'boot-b'}


class ClusterControlTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='telegent-control-')
        self.path=Path(self.temp.name)/'authority.db'
        self.now=[2000000000.0]
        self.control=Coordinator(str(self.path),clock=lambda:self.now[0],lease_seconds=10)
        self.control.bootstrap(OPERATOR)
        for node,token in TOKENS.items():
            self.control.add_node(OPERATOR,node,token)
            self.control.open_session(token,INSTANCES[node])
            self.control.heartbeat(token,INSTANCES[node],True,1024**3)

    def tearDown(self):
        self.control.close();self.temp.cleanup()

    def error(self,code,fn,*args,**kwargs):
        with self.assertRaises(ControlError) as caught:fn(*args,**kwargs)
        self.assertEqual(caught.exception.code,code,str(caught.exception))
        for token in [OPERATOR,*TOKENS.values()]:self.assertNotIn(token,str(caught.exception))

    def claim(self,node='node-a'):
        return self.control.claim(TOKENS[node],INSTANCES[node])

    def append(self,lease,key='request-a',payload=None,base=0):
        return self.control.append(TOKENS[lease['node']],lease['instance'],lease['epoch'],base,key,
                                   {'operation':'fixture','value':1} if payload is None else payload)

    def ack(self,node,receipt):
        return self.control.acknowledge(TOKENS[node],INSTANCES[node],receipt['index'],receipt['hash'])

    def test_both_devices_can_have_internet_but_only_one_gets_authority(self):
        # Internet reachability is independent of the mutually exclusive durable claim.
        internet={'node-a':True,'node-b':True}
        def attempt(node):
            self.assertTrue(internet[node])
            try:return self.claim(node)
            except ControlError as error:return error.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(attempt,TOKENS))
        self.assertEqual(sum(isinstance(result,dict) for result in results),1)
        self.assertIn(409,results)

    def test_atomic_claim_also_excludes_a_second_coordinator_connection(self):
        other=Coordinator(str(self.path),clock=lambda:self.now[0],lease_seconds=10)
        try:
            def attempt(item):
                store,node=item
                try:return store.claim(TOKENS[node],INSTANCES[node])
                except ControlError as error:return error.code
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(attempt,[(self.control,'node-a'),(other,'node-b')]))
            self.assertEqual(sum(isinstance(result,dict) for result in results),1)
            self.assertIn(409,results)
        finally:other.close()

    def test_successor_must_recover_the_committed_history_before_claiming(self):
        lease=self.claim();receipt=self.append(lease)
        self.now[0]+=11
        self.control.heartbeat(TOKENS['node-b'],INSTANCES['node-b'],True,1024**3)
        self.error(425,self.claim,'node-b')
        entries=self.control.entries(TOKENS['node-b'],INSTANCES['node-b'],0)
        self.assertEqual(entries[0]['payload'],{'operation':'fixture','value':1})
        self.ack('node-b',receipt)
        successor=self.claim('node-b')
        self.assertGreater(successor['epoch'],lease['epoch'])
        self.error(409,self.append,lease,key='stale-request',base=1)
        self.assertEqual(self.control.state(OPERATOR)['head'],1)

    def test_lost_append_reply_retries_once_even_after_leadership_changes(self):
        lease=self.claim();receipt=self.append(lease)
        repeated=self.append(lease)
        self.assertTrue(repeated['duplicate']);self.assertEqual(repeated['index'],receipt['index'])
        self.now[0]+=11;self.ack('node-b',receipt)
        self.control.heartbeat(TOKENS['node-b'],INSTANCES['node-b'],True,1024**3)
        self.claim('node-b')
        repeated=self.append(lease)
        self.assertTrue(repeated['duplicate'])
        self.assertEqual(self.control.state(OPERATOR)['head'],1)
        self.error(409,self.append,lease,payload={'operation':'different'})

    def test_old_base_is_refused_without_creating_a_second_history(self):
        lease=self.claim();receipt=self.append(lease);self.ack('node-a',receipt)
        self.error(409,self.append,lease,key='request-b',base=0)
        next_receipt=self.append(lease,key='request-b',base=1)
        self.assertEqual(next_receipt['index'],2)
        entries=self.control.entries(TOKENS['node-a'],INSTANCES['node-a'],0)
        self.assertEqual(entries[0]['previous_hash'],GENESIS)
        self.assertEqual(entries[1]['previous_hash'],entries[0]['entry_hash'])

    def test_append_requires_the_previous_commit_to_be_applied(self):
        lease=self.claim();receipt=self.append(lease)
        self.error(425,self.append,lease,key='request-b',base=1)
        self.ack('node-a',receipt)
        self.assertEqual(self.append(lease,key='request-b',base=1)['index'],2)

    def test_expired_or_unready_leader_cannot_keep_writing(self):
        lease=self.claim();self.now[0]+=11
        self.error(409,self.append,lease)
        self.control.heartbeat(TOKENS['node-a'],INSTANCES['node-a'],True,1024**3)
        fresh=self.claim()
        self.assertGreater(fresh['epoch'],lease['epoch'])
        self.control.heartbeat(TOKENS['node-a'],INSTANCES['node-a'],False,1024**3)
        self.error(409,self.append,fresh)
        self.assertIsNone(self.control.state(OPERATOR)['owner'])

    def test_process_restart_fences_old_incarnation_and_requires_readiness_again(self):
        lease=self.claim();receipt=self.append(lease)
        self.control.open_session(TOKENS['node-a'],'new-boot',previous_instance=INSTANCES['node-a'])
        self.error(409,self.append,lease)
        self.error(409,self.control.heartbeat,TOKENS['node-a'],INSTANCES['node-a'],True,1024**3)
        self.error(425,self.control.claim,TOKENS['node-a'],'new-boot')
        self.control.heartbeat(TOKENS['node-a'],'new-boot',True,1024**3)
        self.error(425,self.control.claim,TOKENS['node-a'],'new-boot')
        self.control.acknowledge(TOKENS['node-a'],'new-boot',receipt['index'],receipt['hash'])
        self.assertGreater(self.control.claim(TOKENS['node-a'],'new-boot')['epoch'],lease['epoch'])
        self.error(409,self.control.open_session,TOKENS['node-a'],'old-request',previous_instance='')

    def test_coordinator_restart_preserves_log_epoch_and_unexpired_owner(self):
        lease=self.claim();receipt=self.append(lease)
        self.control.close()
        self.control=Coordinator(str(self.path),clock=lambda:self.now[0],lease_seconds=10)
        state=self.control.state(OPERATOR)
        self.assertEqual((state['head'],state['head_hash']),(receipt['index'],receipt['hash']))
        self.assertEqual(state['epoch'],lease['epoch'])
        self.ack('node-b',receipt)
        self.error(409,self.claim,'node-b')
        self.error(409,self.control.bootstrap,OPERATOR)
        self.assertTrue(self.append(lease)['duplicate'])
        self.assertEqual(self.control.db.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_acknowledgement_is_monotonic_and_checked_against_the_journal(self):
        lease=self.claim();receipt=self.append(lease);self.ack('node-a',receipt)
        self.control.acknowledge(TOKENS['node-a'],INSTANCES['node-a'],0,GENESIS)
        self.error(409,self.control.acknowledge,TOKENS['node-b'],INSTANCES['node-b'],1,'0'*64)
        self.error(409,self.control.acknowledge,TOKENS['node-b'],INSTANCES['node-b'],2,'0'*64)
        self.error(409,self.control.entries,TOKENS['node-b'],INSTANCES['node-b'],2)
        states={row['id']:row for row in self.control.state(OPERATOR)['nodes']}
        self.assertEqual(states['node-a']['applied'],1)
        self.assertEqual(states['node-b']['applied'],0)

    def test_only_operator_enrolls_or_revokes_and_node_revocation_fences_immediately(self):
        self.error(403,self.control.add_node,TOKENS['node-a'],'node-c','c'*48)
        self.error(403,self.control.revoke,TOKENS['node-a'],'node-b')
        lease=self.claim();self.control.revoke(OPERATOR,'node-a')
        self.error(401,self.append,lease)
        successor=self.claim('node-b')
        self.assertGreater(successor['epoch'],lease['epoch'])
        self.error(403,self.control.claim,OPERATOR,'boot-operator')

    def test_app_tokens_are_not_cluster_keys_and_never_appear_in_state_or_errors(self):
        for token in ('tgt_'+'a'*48,'tgs_'+'b'*48):self.error(401,self.control.state,token)
        state=str(self.control.state(OPERATOR))
        for token in [OPERATOR,*TOKENS.values()]:
            self.assertNotIn(token,state)
            self.assertNotIn(hashlib.sha256(token.encode()).hexdigest(),state)

    def test_invalid_requests_do_not_advance_history_or_change_authority(self):
        lease=self.claim()
        for payload in ({'value':float('nan')},{'value':object()},{'nested':{2:'a',10:'b'}}):
            self.error(400,self.append,lease,payload=payload)
        self.error(400,self.append,lease,key='../bad')
        self.error(400,self.control.heartbeat,TOKENS['node-a'],INSTANCES['node-a'],True,-1)
        self.assertEqual(self.control.state(OPERATOR)['head'],0)

    def test_invalid_clock_stops_mutations_without_guessing_a_new_owner(self):
        lease=self.claim();self.now[0]=float('nan')
        self.error(503,self.append,lease)
        self.error(503,self.claim,'node-b')
        self.assertEqual(self.control.state(OPERATOR)['owner'],'node-a')

    def test_replica_rejects_tampered_payload_sequence_and_epoch(self):
        lease=self.claim();receipt=self.append(lease);self.ack('node-a',receipt)
        second=self.append(lease,key='request-b',base=1)
        entries=self.control.entries(TOKENS['node-b'],INSTANCES['node-b'],0)
        self.assertEqual(verify_entries(entries,0,GENESIS),(second['index'],second['hash']))
        for change in ({'payload':{'value':'modified'}},{'idx':9},{'epoch':99},{'previous_hash':'1'*64},
                       {'entry_hash':'2'*64},{'payload_hash':'3'*64},{'node':'node-c'}):
            altered=[dict(entries[0],**change),entries[1]]
            self.error(409,verify_entries,altered,0,GENESIS)
        self.error(409,verify_entries,entries[1:],0,GENESIS)

    def test_committed_entry_survives_abrupt_process_exit_without_closing_database(self):
        path=Path(self.temp.name)/'abrupt.db'
        script='''import json, os, sys
sys.path.insert(0,sys.argv[1])
from cluster_control import Coordinator
data=json.loads(sys.stdin.read())
c=Coordinator(data['path'],clock=lambda:2000000000.0,lease_seconds=10)
c.bootstrap(data['operator']);c.add_node(data['operator'],'node-a',data['node'])
c.open_session(data['node'],'boot-a');c.heartbeat(data['node'],'boot-a',True,1000000000)
lease=c.claim(data['node'],'boot-a')
result=c.append(data['node'],'boot-a',lease['epoch'],0,'request-a',{'operation':'fixture'})
print(json.dumps(result),flush=True)
os._exit(0)
'''
        env={k:v for k,v in os.environ.items() if not k.startswith(('TELEGENT_','CODEX_','CLAUDE','KIMI'))}
        env.update(HOME=self.temp.name,USERPROFILE=self.temp.name)
        child=subprocess.run([sys.executable,'-X','utf8','-c',script,str(Path(__file__).resolve().parent.parent)],
            input=json.dumps({'path':str(path),'operator':OPERATOR,'node':TOKENS['node-a']}),
            capture_output=True,text=True,encoding='utf-8',env=env,timeout=10)
        self.assertEqual(child.returncode,0,child.stderr)
        receipt=json.loads(child.stdout)
        reopened=Coordinator(str(path),clock=lambda:self.now[0],lease_seconds=10)
        try:
            state=reopened.state(OPERATOR)
            self.assertEqual((state['head'],state['head_hash']),(receipt['index'],receipt['hash']))
            entries=reopened.entries(TOKENS['node-a'],'boot-a',0)
            self.assertEqual(verify_entries(entries,0,GENESIS),(receipt['index'],receipt['hash']))
        finally:reopened.close()


if __name__=='__main__':unittest.main(verbosity=2)
