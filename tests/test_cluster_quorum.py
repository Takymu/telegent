"""Persistent equal-peer Paxos acceptors for trust-changing operations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cluster_peer import GENESIS, PeerError
from cluster_quorum import QuorumLog, choose_value


MEMBERS = ("node-a", "node-b", "node-c")


class ClusterQuorumTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="telegent-quorum-")
        self.logs = {node: QuorumLog(str(Path(self.temp.name) / (node + ".db")),
                                     "quorum-cluster", node, MEMBERS) for node in MEMBERS}

    def tearDown(self):
        for log in self.logs.values():
            log.close()
        self.temp.cleanup()

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(PeerError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def round(self, proposer, counter, value, participants=("node-a", "node-b", "node-c")):
        state = self.logs[proposer].state()
        slot, previous = state["head"] + 1, state["head_hash"]
        prepared = [self.logs[node].prepare(slot, previous, counter, proposer) for node in participants]
        selected = choose_value(prepared, MEMBERS, value)
        accepted = [self.logs[node].accept(slot, previous, counter, proposer, selected) for node in participants]
        return slot, previous, selected, accepted

    def test_any_phone_can_propose_but_commit_needs_majority(self):
        for index, proposer in enumerate(MEMBERS, 1):
            value = {"kind": "project.permission", "account": "account-a", "role": "member",
                     "proposal": index}
            participants = tuple(node for node in MEMBERS if node != MEMBERS[index % len(MEMBERS)])
            slot, previous, selected, accepted = self.round(proposer, index, value, participants)
            receipts = [dict(item) for item in accepted]
            results = [log.commit(slot, previous, selected, receipts) for log in self.logs.values()]
            self.assertTrue(all(result["hash"] == results[0]["hash"] for result in results))
        self.assertEqual(len(self.logs["node-c"].entries()), len(MEMBERS))

    def test_two_node_cluster_requires_both_phones_for_trust_change(self):
        paths = [str(Path(self.temp.name) / ("two-%s.db" % node)) for node in ("node-x", "node-y")]
        logs = [QuorumLog(path, "two-cluster", node, ("node-x", "node-y"))
                for path, node in zip(paths, ("node-x", "node-y"))]
        try:
            response = logs[0].prepare(1, GENESIS, 1, "node-x")
            self.error(425, choose_value, [response], ("node-x", "node-y"), {"kind": "token.reset"})
            selected = choose_value([response, logs[1].prepare(1, GENESIS, 1, "node-x")],
                                    ("node-x", "node-y"), {"kind": "token.reset"})
            receipts = [log.accept(1, GENESIS, 1, "node-x", selected) for log in logs]
            for log in logs:
                log.commit(1, GENESIS, selected, receipts)
            self.assertEqual(logs[1].state()["head"], 1)
        finally:
            for log in logs:
                log.close()

    def test_new_proposer_must_recover_value_accepted_before_old_proposer_crashed(self):
        original = {"kind": "account.token_reset", "account": "account-a", "generation": 2}
        prepared = [self.logs[node].prepare(1, GENESIS, 1, "node-a") for node in ("node-a", "node-b")]
        selected = choose_value(prepared, MEMBERS, original)
        for node in ("node-a", "node-b"):
            self.logs[node].accept(1, GENESIS, 1, "node-a", selected)

        recovery = [self.logs[node].prepare(1, GENESIS, 2, "node-c") for node in ("node-b", "node-c")]
        chosen = choose_value(recovery, MEMBERS, {"kind": "different"})
        self.assertEqual(chosen, original)
        receipts = [self.logs[node].accept(1, GENESIS, 2, "node-c", chosen)
                    for node in ("node-b", "node-c")]
        for log in self.logs.values():
            log.commit(1, GENESIS, chosen, receipts)
        self.assertTrue(all(log.entries()[0]["value"] == original for log in self.logs.values()))

    def test_old_ballot_and_two_values_in_one_ballot_are_rejected(self):
        self.logs["node-a"].prepare(1, GENESIS, 3, "node-c")
        self.error(409, self.logs["node-a"].prepare, 1, GENESIS, 2, "node-b")
        self.error(409, self.logs["node-a"].accept, 1, GENESIS, 2, "node-b", {"value": 1})
        self.logs["node-a"].accept(1, GENESIS, 3, "node-c", {"value": 1})
        self.error(409, self.logs["node-a"].accept, 1, GENESIS, 3, "node-c", {"value": 2})

    def test_next_slot_requires_previous_commit_and_matching_hash(self):
        slot, previous, selected, receipts = self.round("node-a", 1, {"kind": "project.rename"},
                                                        ("node-a", "node-b"))
        for log in self.logs.values():
            log.commit(slot, previous, selected, receipts)
        head = self.logs["node-a"].state()
        self.error(409, self.logs["node-a"].prepare, 2, GENESIS, 2, "node-a")
        self.assertEqual(self.logs["node-a"].prepare(2, head["head_hash"], 2, "node-a")["slot"], 2)

    def test_receipts_must_be_distinct_same_ballot_and_same_value(self):
        slot, previous, selected, receipts = self.round("node-a", 1, {"kind": "invite.create"},
                                                        ("node-a", "node-b"))
        self.error(425, self.logs["node-c"].commit, slot, previous, selected, receipts[:1])
        duplicate = [receipts[0], receipts[0]]
        self.error(409, self.logs["node-c"].commit, slot, previous, selected, duplicate)
        changed = [dict(receipts[0]), dict(receipts[1], value_hash="f" * 64)]
        self.error(409, self.logs["node-c"].commit, slot, previous, selected, changed)

    def test_accepted_value_and_promise_survive_abrupt_process_exit(self):
        path = str(Path(self.temp.name) / "abrupt.db")
        script = """import json,os,sys
sys.path.insert(0,sys.argv[1])
from cluster_quorum import QuorumLog
d=json.loads(sys.stdin.read())
q=QuorumLog(d['path'],'abrupt-cluster','node-a',('node-a','node-b','node-c'))
q.prepare(1,'0'*64,7,'node-b')
print(json.dumps(q.accept(1,'0'*64,7,'node-b',{'kind':'permission'})),flush=True)
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
        reopened = QuorumLog(path, "abrupt-cluster", "node-a", MEMBERS)
        try:
            response = reopened.prepare(1, GENESIS, 8, "node-c")
            self.assertEqual(response["accepted"]["value"], {"kind": "permission"})
            self.assertEqual(reopened.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
