"""Node runtime configuration, direct pulls and the optional real HTTP route."""
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cluster_node import ClusterNode
from cluster_peer import PeerError
import server


NODES = ("node-a", "node-b", "node-c")


class ClusterNodeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="telegent-node-")
        self.root = Path(self.temp.name)
        self.keys = {}
        for left_index, left in enumerate(NODES):
            for right in NODES[left_index + 1:]:
                key = ("pair-%s-%s-" % (left, right)).encode() + b"x" * 48
                self.keys[frozenset((left, right))] = key
        self.nodes = {node: self.make_node(node) for node in NODES}

    def tearDown(self):
        server.CLUSTER_NODE = None
        for node in self.nodes.values():
            node.close()
        self.temp.cleanup()

    def key(self, left, right):
        return self.keys[frozenset((left, right))]

    def key_file(self, left, right):
        directory = self.root / left
        directory.mkdir(exist_ok=True)
        path = directory / ("pair-%s.key" % right)
        if not path.exists():
            path.write_bytes(self.key(left, right) + b"\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return path

    def settings(self, node, urls=None):
        urls = urls or {}
        peers = []
        for peer in NODES:
            if peer == node:
                continue
            peers.append({"id": peer, "url": urls.get(peer, "https://%s.invalid" % peer),
                          "key_file": str(self.key_file(node, peer))})
        return {"id": "node-cluster", "node": node, "journal": str(self.root / (node + ".db")),
                "peers": peers, "poll_seconds": 10,
                "allow_http_localhost": any(url.startswith("http://") for url in urls.values())}

    def make_node(self, node, urls=None):
        return ClusterNode(self.settings(node, urls), str(self.root), clock=lambda: 2000000000)

    def exchange(self, peer, body, headers):
        return self.nodes[peer].handle_sync(body, headers)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(PeerError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def test_equal_nodes_pull_both_directions_and_relay_a_dead_origin(self):
        self.nodes["node-a"].journal.append("event-a", "message.create", {"body": "a"})
        self.nodes["node-b"].journal.append("event-b", "message.create", {"body": "b"})
        self.assertEqual(self.nodes["node-a"].pull("node-b", self.exchange), 1)
        self.assertEqual(self.nodes["node-c"].pull("node-a", self.exchange), 2)
        self.assertEqual(self.nodes["node-b"].pull("node-c", self.exchange), 1)
        operations = [[event["operation_id"] for event in node.journal.operations()]
                      for node in self.nodes.values()]
        self.assertEqual(operations[0], operations[1])
        self.assertEqual(operations[1], operations[2])

    def test_tampered_response_and_wrong_pair_key_are_rejected(self):
        self.nodes["node-b"].journal.append("event-b", "message.create", {"body": "b"})

        def tampered(peer, body, headers):
            response, response_headers = self.exchange(peer, body, headers)
            return response + b" ", response_headers

        self.error(401, self.nodes["node-a"].pull, "node-b", tampered)
        original = self.nodes["node-a"].peers["node-b"]["key"]
        self.nodes["node-a"].peers["node-b"]["key"] = b"wrong-" + b"z" * 48
        try:
            self.error(401, self.nodes["node-a"].pull, "node-b", self.exchange)
        finally:
            self.nodes["node-a"].peers["node-b"]["key"] = original

    def test_status_contains_no_keys_and_records_failures_without_stopping_other_peers(self):
        self.nodes["node-a"]._record("node-b", PeerError(503, "offline"))
        self.nodes["node-a"]._record("node-c")
        report = self.nodes["node-a"].report()
        self.assertEqual(report["peers"]["node-b"]["failures"], 1)
        self.assertIsNotNone(report["peers"]["node-c"]["last_success"])
        rendered = str(report)
        for key in self.keys.values():
            self.assertNotIn(key.decode(), rendered)

    def test_configuration_rejects_plain_remote_http_missing_key_and_extra_fields(self):
        settings = self.settings("node-a")
        settings["peers"][0]["url"] = "http://example.invalid"
        self.error(400, ClusterNode, settings, str(self.root))
        settings = self.settings("node-a")
        settings["peers"][0]["key_file"] = str(self.root / "missing.key")
        self.error(500, ClusterNode, settings, str(self.root))
        settings = self.settings("node-a")
        settings["unexpected"] = True
        self.error(400, ClusterNode, settings, str(self.root))

    def test_real_server_route_exchanges_a_signed_page_over_local_http(self):
        self.nodes["node-b"].journal.append("event-http", "message.create", {"body": "http"})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        server.CLUSTER_NODE = self.nodes["node-b"]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        old = self.nodes.pop("node-a")
        old.close()
        url = "http://127.0.0.1:%d" % httpd.server_address[1]
        self.nodes["node-a"] = self.make_node("node-a", {"node-b": url})
        try:
            self.assertEqual(self.nodes["node-a"].pull("node-b"), 1)
            self.assertEqual(self.nodes["node-a"].journal.operations()[0]["payload"], {"body": "http"})
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            server.CLUSTER_NODE = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
