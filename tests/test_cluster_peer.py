"""Symmetric peer journal: partitions, convergence, deduplication and safety boundary."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cluster_peer import GENESIS, PeerError, PeerJournal, has_quorum, operation_mode, quorum_size


MEMBERS = ("node-a", "node-b", "node-c")


class PeerJournalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="telegent-peer-")
        self.journals = {
            node: PeerJournal(str(Path(self.temp.name) / (node + ".db")), "cluster-fixture", node, MEMBERS)
            for node in MEMBERS
        }

    def tearDown(self):
        for journal in self.journals.values():
            journal.close()
        self.temp.cleanup()

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(PeerError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def relay(self, source, target, origin=None):
        origin = source if origin is None else origin
        after = self.journals[target].frontier()[origin]["sequence"]
        return self.journals[target].ingest(self.journals[source].export(origin, after))

    def test_two_isolated_nodes_accept_messages_and_converge_without_a_leader(self):
        left, duplicate = self.journals["node-a"].append(
            "send-left", "message.create", {"id": "message-left", "body": "left"})
        right, duplicate_right = self.journals["node-b"].append(
            "send-right", "message.create", {"id": "message-right", "body": "right"})
        self.assertFalse(duplicate or duplicate_right)
        self.assertEqual(left["previous_hash"], GENESIS)
        self.assertEqual(right["previous_hash"], GENESIS)
        self.relay("node-a", "node-b")
        self.relay("node-b", "node-a")
        self.assertEqual([event["operation_id"] for event in self.journals["node-a"].operations()],
                         [event["operation_id"] for event in self.journals["node-b"].operations()])
        self.assertEqual(len(self.journals["node-a"].operations()), 2)

    def test_same_request_and_body_on_both_partitions_materializes_once(self):
        payload = {"id": "stable-message", "body": "retry"}
        first, _ = self.journals["node-a"].append("stable-key", "message.create", payload)
        second, _ = self.journals["node-b"].append("stable-key", "message.create", payload)
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.relay("node-a", "node-c")
        self.relay("node-b", "node-c")
        self.assertEqual(len(self.journals["node-c"].operations()), 1)
        self.assertEqual(self.journals["node-c"].conflicts(), [])

    def test_same_request_with_different_body_is_quarantined_not_guessed(self):
        self.journals["node-a"].append("bad-retry", "message.create", {"body": "one"})
        self.journals["node-b"].append("bad-retry", "message.create", {"body": "two"})
        self.relay("node-a", "node-c")
        self.relay("node-b", "node-c")
        self.assertEqual(self.journals["node-c"].operations(), [])
        self.assertEqual(self.journals["node-c"].conflicts()[0]["request_key"], "bad-retry")

    def test_retry_on_one_node_is_idempotent_and_changed_retry_is_rejected(self):
        payload = {"message": "hello"}
        first, duplicate = self.journals["node-a"].append("request-one", "message.create", payload)
        second, duplicate = self.journals["node-a"].append("request-one", "message.create", payload)
        self.assertTrue(duplicate)
        self.assertEqual(first, second)
        self.error(409, self.journals["node-a"].append,
                   "request-one", "message.create", {"message": "changed"})

    def test_rights_tokens_membership_and_unknown_operations_never_enter_partition_log(self):
        for kind in ("account.token_reset", "project.permission", "cluster.membership", "future.operation"):
            self.assertEqual(operation_mode(kind), "quorum")
            self.error(425, self.journals["node-a"].append, "request-" + kind, kind, {"value": 1})
        self.assertEqual(self.journals["node-a"].frontier()["node-a"]["sequence"], 0)

    def test_tampering_gap_and_origin_fork_are_rejected(self):
        first, _ = self.journals["node-a"].append("request-a", "message.create", {"body": "one"})
        second, _ = self.journals["node-a"].append("request-b", "message.create", {"body": "two"})
        self.error(409, self.journals["node-b"].ingest, [second])
        altered = copy.deepcopy(first)
        altered["payload"]["body"] = "altered"
        self.error(409, self.journals["node-b"].ingest, [altered])
        self.assertEqual(self.relay("node-a", "node-b"), 2)
        duplicate_with_changed_body = copy.deepcopy(first)
        duplicate_with_changed_body["payload"]["body"] = "not-the-stored-body"
        self.error(409, self.journals["node-b"].ingest, [duplicate_with_changed_body])
        fork = copy.deepcopy(first)
        fork["event_hash"] = "f" * 64
        self.error(409, self.journals["node-b"].ingest, [fork])

    def test_third_node_can_relay_an_origin_chain_after_source_disappears(self):
        self.journals["node-a"].append("source-event", "reaction.set", {"message": "m", "emoji": "😁"})
        self.relay("node-a", "node-b")
        self.assertEqual(self.relay("node-b", "node-c", origin="node-a"), 1)
        self.assertEqual(self.journals["node-a"].frontier(), self.journals["node-c"].frontier())

    def test_quorum_math_does_not_mistake_internet_access_for_peer_votes(self):
        self.assertEqual(quorum_size(2), 2)
        self.assertEqual(quorum_size(3), 2)
        self.assertFalse(has_quorum(("node-a", "node-b"), {"node-a"}))
        self.assertTrue(has_quorum(MEMBERS, {"node-a", "node-c"}))
        self.assertFalse(has_quorum(MEMBERS, {"node-a", "google"}))

    def test_invalid_payloads_and_foreign_cluster_do_not_change_frontier(self):
        for payload in ({"bad": float("nan")}, {2: "bad"}, {"bad": object()}):
            self.error(400, self.journals["node-a"].append,
                       "invalid-payload", "message.create", payload)
        event, _ = self.journals["node-a"].append("valid-payload", "message.create", {"body": "ok"})
        event["cluster"] = "foreign-cluster"
        self.error(403, self.journals["node-b"].ingest, [event])
        self.assertEqual(self.journals["node-b"].frontier()["node-a"]["sequence"], 0)

    def test_database_identity_and_membership_cannot_be_silently_reused(self):
        path = str(Path(self.temp.name) / "identity.db")
        journal = PeerJournal(path, "cluster-one", "node-a", ("node-a", "node-b"))
        journal.close()
        self.error(409, PeerJournal, path, "cluster-two", "node-a", ("node-a", "node-b"))
        self.error(409, PeerJournal, path, "cluster-one", "node-b", ("node-a", "node-b"))

    def test_committed_event_survives_abrupt_process_exit(self):
        path = str(Path(self.temp.name) / "abrupt.db")
        script = """import json,os,sys
sys.path.insert(0,sys.argv[1])
from cluster_peer import PeerJournal
d=json.loads(sys.stdin.read())
j=PeerJournal(d['path'],'cluster-abrupt','node-a',('node-a','node-b'))
event,_=j.append('request-a','message.create',{'body':'durable'})
print(json.dumps(event),flush=True)
os._exit(0)
"""
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("TELEGENT_", "CODEX_", "CLAUDE", "KIMI"))}
        env.update(HOME=self.temp.name, USERPROFILE=self.temp.name)
        child = subprocess.run([sys.executable, "-X", "utf8", "-c", script,
                                str(Path(__file__).resolve().parent.parent)],
                               input=json.dumps({"path": path}), capture_output=True, text=True,
                               encoding="utf-8", env=env, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        event = json.loads(child.stdout)
        reopened = PeerJournal(path, "cluster-abrupt", "node-a", ("node-a", "node-b"))
        try:
            self.assertEqual(reopened.frontier()["node-a"]["hash"], event["event_hash"])
            self.assertEqual(reopened.operations()[0]["payload"], {"body": "durable"})
            self.assertEqual(reopened.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
