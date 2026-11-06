"""Authenticated-RPC-agnostic proposer, learner catch-up and quorum loss."""
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cluster_consensus import Consensus
from cluster_peer import GENESIS, PeerError
from cluster_quorum import QuorumLog


MEMBERS = ("node-a", "node-b", "node-c")


class ConsensusTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="telegent-consensus-")
        self.logs = {node: QuorumLog(str(Path(self.temp.name) / (node + ".db")),
                                     "consensus-cluster", node, MEMBERS) for node in MEMBERS}

    def tearDown(self):
        for log in self.logs.values(): log.close()
        self.temp.cleanup()

    def exchange(self, peer, action, payload):
        log = self.logs[peer]
        if action == "prepare": return log.prepare(**payload)
        if action == "accept": return log.accept(**payload)
        if action == "commit": return log.commit(payload["slot"], payload["previous_hash"],
                                                   payload["value"], payload["receipts"])
        if action == "entries": return {"entries": log.entries(payload["after"], payload["limit"])}
        raise AssertionError(action)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(PeerError) as caught: function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def test_majority_commits_and_offline_phone_verifiably_catches_up(self):
        protocol = Consensus(self.logs["node-a"], self.exchange)
        value = {"kind": "project.permission", "account": "account-a", "role": "member"}
        result = protocol.propose(value, peers=("node-a", "node-b"))
        self.assertEqual(result["value"], value)
        self.assertEqual(self.logs["node-c"].state()["head"], 0)
        learned = Consensus(self.logs["node-c"], self.exchange).catch_up("node-a")
        self.assertEqual(learned, 1)
        self.assertEqual(self.logs["node-c"].entries()[0]["value"], value)

    def test_new_proposer_recovers_value_accepted_before_crash(self):
        original = {"kind": "account.token_reset", "generation": 2}
        for node in ("node-a", "node-b"):
            self.logs[node].prepare(1, GENESIS, 10, "node-a")
            self.logs[node].accept(1, GENESIS, 10, "node-a", original)
        result = Consensus(self.logs["node-c"], self.exchange).propose(
            {"kind": "different"}, peers=("node-b", "node-c"), minimum_ballot=11)
        self.assertEqual(result["value"], original)
        self.assertEqual(self.logs["node-b"].entries()[0]["value"], original)

    def test_two_node_cluster_does_not_change_trust_with_one_phone(self):
        members = ("node-x", "node-y")
        logs = {node: QuorumLog(str(Path(self.temp.name) / (node + ".db")), "two-cluster", node, members)
                for node in members}
        try:
            protocol = Consensus(logs["node-x"], lambda peer, action, payload: (_ for _ in ()).throw(PeerError(503, "offline")))
            self.error(425, protocol.propose, {"kind": "membership"}, peers=("node-x",))
            self.assertEqual(logs["node-x"].state()["head"], 0)
        finally:
            for log in logs.values(): log.close()

    def test_tampered_certificate_or_value_cannot_be_learned(self):
        Consensus(self.logs["node-a"], self.exchange).propose({"kind": "invite.create"},
                                                               peers=("node-a", "node-b"))
        entry = self.logs["node-a"].entries()[0]
        entry["receipts"][0]["value_hash"] = "f" * 64
        self.error(409, self.logs["node-c"].learn, entry)
        self.assertEqual(self.logs["node-c"].state()["head"], 0)


if __name__ == "__main__": unittest.main(verbosity=2)
