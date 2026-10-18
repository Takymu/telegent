"""Owner protocol decisions, inherited settings and at-most-once dispatch."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('owner_transport', Path(__file__).resolve().parents[1]/'tools/codex_owner_transport.py')
ipc = importlib.util.module_from_spec(spec); spec.loader.exec_module(ipc)


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'receipt.json'
        self.binding = {'thread': 'fixture-thread', 'cwd': self.tmp.name}
        self.notice = {'id': 'fixture-id', 'text': 'metadata only', 'input': [{'type':'text','text':'metadata only'}]}
        self.calls = []

    def owner(self, inactive=False, uncertain=False):
        outer = self
        class Fake:
            def request(self, method, params, version=0, target=None):
                outer.calls.append((method,params,version,target))
                if method == 'thread-owner-discovery':
                    return {'handledByClientId':'right-owner'}
                outer.assertEqual(target,'right-owner')
                if method == 'thread-follower-steer-turn':
                    if uncertain:raise ipc.RpcError('request-timeout')
                    if inactive:raise ipc.RpcError('inactive')
                return {'result':{'result':{'turnId':'fixture-turn'}}}
        return Fake()

    def test_active_steers_without_starting_or_queueing(self):
        result=ipc.dispatch(self.owner(),self.binding,self.notice,self.path)
        self.assertEqual(result['mode'],'steer');self.assertEqual(result['status'],'accepted')
        self.assertEqual([c[0] for c in self.calls],['thread-owner-discovery','thread-follower-steer-turn'])
        self.assertNotIn('serverQueuedMessageId',str(self.calls))

    def test_idle_starts_with_inherited_thread_settings(self):
        result=ipc.dispatch(self.owner(inactive=True),self.binding,self.notice,self.path)
        self.assertEqual(result['mode'],'start')
        method,params,version,target=self.calls[-1]
        self.assertEqual(method,'thread-follower-start-turn');self.assertEqual(version,2)
        start=params['turnStart'];self.assertTrue(start['context']['inheritThreadSettings'])
        self.assertEqual(set(start['context']),{'inheritThreadSettings','restoreMessage'})
        self.assertEqual(set(start['request']),{'threadId','input','clientUserMessageId'})

    def test_ambiguous_timeout_never_replays_or_falls_back_to_start(self):
        with self.assertRaises(ipc.RpcError):ipc.dispatch(self.owner(uncertain=True),self.binding,self.notice,self.path)
        self.assertEqual(ipc.load_json(self.path)['status'],'submitting')
        count=len(self.calls)
        with self.assertRaises(ipc.RpcError):ipc.dispatch(self.owner(),self.binding,self.notice,self.path)
        self.assertEqual(len(self.calls),count)

    def test_restart_accepted_receipt_never_resubmits(self):
        ipc.dispatch(self.owner(),self.binding,self.notice,self.path)
        count=len(self.calls)
        self.assertEqual(ipc.dispatch(self.owner(),self.binding,self.notice,self.path)['turn_id'],'fixture-turn')
        self.assertEqual(len(self.calls),count)

    def test_foreign_thread_receipt_rejected(self):
        ipc.write_receipt(self.path,{'thread':'foreign','status':'accepted'})
        with self.assertRaises(ipc.RpcError):ipc.dispatch(self.owner(),self.binding,self.notice,self.path)
        self.assertFalse(self.calls)


if __name__ == '__main__':unittest.main(verbosity=2)
