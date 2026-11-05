"""Late replicated messages use an arrival cursor independent of their stable id."""
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server
from cluster_blob import BlobStore
from cluster_peer import PeerJournal
from cluster_app import drain_outbox


class ClusterArrivalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="telegent-arrival-")
        self.old = (server.Cfg.db, server.Cfg.cluster, server._db, server.LAST_ID[0], server.CLUSTER_NODE)
        server.Cfg.db = str(Path(self.temp.name) / "app.db")
        server.Cfg.cluster = {"slot": 7}
        server._db = None
        self.con = server.db()
        journal = PeerJournal(str(Path(self.temp.name) / "events.db"), "arrival-cluster", "node-a",
                              ("node-a", "node-b"))
        self.node = types.SimpleNamespace(slot=7, journal=journal,
                                          blobs=BlobStore(str(Path(self.temp.name) / "blobs"), reserve_bytes=0),
                                          stop_event=threading.Event())
        server.CLUSTER_NODE = self.node
        self.account = server.new_account(self.con, "alice", "human", None, "tgt_" + "a" * 48)
        project_id = server.create_project_row(self.con, "fixture", "fixture", self.account)
        self.project = self.con.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        server.add_member(self.con, self.project["id"], self.account, "owner", self.account)

    def tearDown(self):
        self.con.close()
        self.node.journal.close()
        server.Cfg.db, server.Cfg.cluster, server._db, server.LAST_ID[0], server.CLUSTER_NODE = self.old
        self.temp.cleanup()

    def insert(self, message_id, body):
        self.con.execute("""INSERT INTO messages
            (id,ts,author,author_kind,owner,host,recipients,topic,type,priority,subject,body,
             reply_to,thread_id,needs_approval,status,result,idem_key,project_id,approver,conversation_id)
            VALUES(?,2000000000,?,'human',?,'web','','','info','normal','',?,NULL,?,0,NULL,NULL,?, ?,NULL,NULL)""",
                         (message_id, self.account, self.account, body, message_id,
                          "key-%d" % message_id, self.project["id"]))

    def test_late_lower_message_id_is_still_returned_after_cursor(self):
        self.insert(500000000000300, "first")
        self.insert(500000000000900, "second")
        first_cursor = server.arrival_cursor(500000000000300)
        second_cursor = server.arrival_cursor(500000000000900)
        self.assertLess(first_cursor, second_cursor)
        self.insert(500000000000500, "late but lower")
        late_cursor = server.arrival_cursor(500000000000500)
        self.assertGreater(late_cursor, second_cursor)
        user = self.con.execute("SELECT * FROM users WHERE name=?", (self.account,)).fetchone()
        rows = server.list_messages(user, {"after": str(second_cursor), "limit": 20, "full": "1"},
                                    self.project["id"])
        self.assertEqual([(row["id"], row["cursor"], row["body"]) for row in rows],
                         [(500000000000500, late_cursor, "late but lower")])

    def test_single_server_mode_keeps_message_id_as_cursor(self):
        self.insert(500000000000300, "cluster")
        self.assertNotEqual(server.arrival_cursor(500000000000300), 500000000000300)
        server.Cfg.cluster = None
        self.assertEqual(server.arrival_cursor(500000000000300), 500000000000300)

    def test_real_message_commit_allocates_global_ids_and_atomic_outbox(self):
        user = self.con.execute("SELECT * FROM users WHERE name=?", (self.account,)).fetchone()
        message_id, duplicate = server.insert_message(
            user, "web", {"body": "cluster message", "idem_key": "fixture-message-key"}, self.project["id"])
        self.assertFalse(duplicate)
        self.assertGreater(message_id, 1000000000000)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM cluster_outbox WHERE exported IS NULL").fetchone()[0], 1)
        self.assertEqual(drain_outbox(self.con, self.node.journal), 1)
        operation = self.node.journal.operations()[0]
        self.assertEqual(operation["payload"]["message"]["id"], message_id)
        self.assertEqual(operation["payload"]["message"]["body"], "cluster message")


if __name__ == "__main__":
    unittest.main(verbosity=2)
