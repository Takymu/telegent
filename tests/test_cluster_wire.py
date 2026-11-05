"""Pairwise authentication and bounded peer journal synchronization."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cluster_peer import PeerError, PeerJournal
from cluster_wire import (SYNC_PATH, apply_sync_response, build_sync_response,
                          decode_sync_request, encode_sync_request, sign_request, sign_response,
                          verify_request, verify_response)


MEMBERS = ("node-a", "node-b", "node-c")
PAIR_AB = b"pair-a-b-" + b"x" * 48
PAIR_AC = b"pair-a-c-" + b"y" * 48


class ClusterWireTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="telegent-wire-")
        self.journals = {
            node: PeerJournal(str(Path(self.temp.name) / (node + ".db")), "wire-cluster", node, MEMBERS)
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

    def signed(self, source, target, key, body, timestamp=2000000000, nonce="nonce-fixture"):
        return sign_request(key, source, target, "POST", SYNC_PATH, body, timestamp, nonce)

    def test_pairwise_signature_authenticates_target_body_and_consumes_nonce_once(self):
        body = encode_sync_request(self.journals["node-a"])
        headers = self.signed("node-a", "node-b", PAIR_AB, body)
        source = verify_request(self.journals["node-b"], {"node-a": PAIR_AB}, "POST", SYNC_PATH,
                                body, headers, now=2000000000)
        self.assertEqual(source, "node-a")
        self.error(409, verify_request, self.journals["node-b"], {"node-a": PAIR_AB},
                   "POST", SYNC_PATH, body, headers, 2000000000)

        changed = body + b" "
        headers2 = self.signed("node-a", "node-b", PAIR_AB, body, nonce="other-nonce")
        self.error(401, verify_request, self.journals["node-b"], {"node-a": PAIR_AB},
                   "POST", SYNC_PATH, changed, headers2, 2000000000)
        self.error(401, verify_request, self.journals["node-c"], {"node-a": PAIR_AC},
                   "POST", SYNC_PATH, body, headers, 2000000000)

    def test_unknown_peer_wrong_key_and_stale_clock_are_rejected_without_secrets_in_errors(self):
        body = encode_sync_request(self.journals["node-a"])
        cases = [
            ({}, self.signed("node-a", "node-b", PAIR_AB, body, nonce="nonce-one"), 2000000000),
            ({"node-a": PAIR_AC}, self.signed("node-a", "node-b", PAIR_AB, body, nonce="nonce-two"), 2000000000),
            ({"node-a": PAIR_AB}, self.signed("node-a", "node-b", PAIR_AB, body,
                                               timestamp=1999999000, nonce="nonce-three"), 2000000000),
        ]
        for keys, headers, now in cases:
            with self.assertRaises(PeerError) as caught:
                verify_request(self.journals["node-b"], keys, "POST", SYNC_PATH, body, headers, now)
            self.assertEqual(caught.exception.code, 401)
            self.assertNotIn(PAIR_AB.decode(), str(caught.exception))

    def test_response_is_bound_to_request_peer_nonce_and_body(self):
        body = b'{"events":[],"frontier":{},"more":false}'
        headers = sign_response(PAIR_AB, "node-b", "node-a", SYNC_PATH, "request-nonce", body)
        self.assertIsNone(verify_response(PAIR_AB, "node-b", "node-a", SYNC_PATH,
                                          "request-nonce", body, headers))
        self.error(401, verify_response, PAIR_AB, "node-b", "node-a", SYNC_PATH,
                   "request-nonce", body + b" ", headers)
        self.error(401, verify_response, PAIR_AB, "node-b", "node-a", SYNC_PATH,
                   "different-nonce", body, headers)

    def test_pull_converges_all_origin_chains_and_third_node_relays_them(self):
        self.journals["node-a"].append("message-a", "message.create", {"body": "from a"})
        self.journals["node-b"].append("message-b", "message.create", {"body": "from b"})

        request = encode_sync_request(self.journals["node-c"])
        frontier = decode_sync_request(request, MEMBERS)
        response_a = build_sync_response(self.journals["node-a"], frontier)
        self.assertEqual(apply_sync_response(self.journals["node-c"], response_a)["inserted"], 1)

        response_b = build_sync_response(
            self.journals["node-b"], decode_sync_request(encode_sync_request(self.journals["node-c"]), MEMBERS))
        self.assertEqual(apply_sync_response(self.journals["node-c"], response_b)["inserted"], 1)

        response_c = build_sync_response(
            self.journals["node-c"], decode_sync_request(encode_sync_request(self.journals["node-a"]), MEMBERS))
        self.assertEqual(apply_sync_response(self.journals["node-a"], response_c)["inserted"], 1)
        self.assertEqual(len(self.journals["node-a"].operations()), 2)

    def test_bounded_pages_resume_from_new_frontier_without_duplicates(self):
        for index in range(5):
            self.journals["node-a"].append("message-%d" % index, "message.create", {"index": index})
        rounds = 0
        while True:
            remote = decode_sync_request(encode_sync_request(self.journals["node-b"]), MEMBERS)
            result = apply_sync_response(self.journals["node-b"],
                                         build_sync_response(self.journals["node-a"], remote, max_events=2))
            rounds += 1
            if not result["more"]:
                break
        self.assertEqual(rounds, 3)
        self.assertEqual(len(self.journals["node-b"].operations()), 5)

    def test_divergent_frontier_and_bad_membership_are_not_treated_as_missing_events(self):
        self.journals["node-a"].append("message-a", "message.create", {"body": "a"})
        remote = self.journals["node-a"].frontier()
        remote["node-a"]["hash"] = "f" * 64
        self.error(409, build_sync_response, self.journals["node-a"], remote)
        malformed = json.dumps({"frontier": {"node-a": {"sequence": 0, "hash": "0" * 64}}}).encode()
        self.error(409, decode_sync_request, malformed, MEMBERS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
