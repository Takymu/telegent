"""JS-safe global ids and crash-safe application outbox integration."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cluster_app import (DeferredOperation, MAX_SAFE_INTEGER, apply_operations,
                         drain_outbox, enqueue, install, next_id)
from cluster_peer import PeerError, PeerJournal


class ClusterApplicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="telegent-app-")
        self.root = Path(self.temp.name)
        self.apps = []
        self.journals = []
        for index, node in enumerate(("node-a", "node-b")):
            connection = sqlite3.connect(str(self.root / (node + "-app.db")), isolation_level=None)
            install(connection, index + 3)
            self.apps.append(connection)
            self.journals.append(PeerJournal(str(self.root / (node + "-events.db")),
                                             "app-cluster", node, ("node-a", "node-b")))

    def tearDown(self):
        for journal in self.journals:
            journal.close()
        for connection in self.apps:
            connection.close()
        self.temp.cleanup()

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(PeerError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def allocate(self, connection, slot, now):
        connection.execute("BEGIN IMMEDIATE")
        try:
            value = next_id(connection, slot, now)
            connection.execute("COMMIT")
            return value
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def test_ids_are_unique_js_safe_and_survive_clock_rollback_and_restart(self):
        ids = []
        for index in range(40):
            ids.append(self.allocate(self.apps[0], 3, 1800000000000 - index))
            ids.append(self.allocate(self.apps[1], 4, 1800000000000 - index))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(0 < value <= MAX_SAFE_INTEGER for value in ids))
        self.assertEqual(ids[0::2], sorted(ids[0::2]))
        self.assertEqual(ids[1::2], sorted(ids[1::2]))
        self.apps[0].close()
        self.apps[0] = sqlite3.connect(str(self.root / "node-a-app.db"), isolation_level=None)
        install(self.apps[0], 3)
        self.assertNotIn(self.allocate(self.apps[0], 3, 1704067200000), ids)

    def test_slot_change_and_allocation_outside_transaction_are_rejected(self):
        self.error(409, install, self.apps[0], 9)
        self.error(500, next_id, self.apps[0], 3, 1800000000000)

    def test_application_commit_and_outbox_are_atomic(self):
        connection = self.apps[0]
        connection.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,body TEXT NOT NULL)")
        connection.execute("BEGIN IMMEDIATE")
        message_id = next_id(connection, 3, 1800000000000)
        connection.execute("INSERT INTO messages VALUES(?,?)", (message_id, "committed"))
        enqueue(connection, "message-key", "message.create", {"id": message_id, "body": "committed"})
        connection.execute("COMMIT")
        connection.execute("BEGIN IMMEDIATE")
        rolled_id = next_id(connection, 3, 1800000000001)
        connection.execute("INSERT INTO messages VALUES(?,?)", (rolled_id, "rolled back"))
        enqueue(connection, "rolled-key", "message.create", {"id": rolled_id, "body": "rolled back"})
        connection.execute("ROLLBACK")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM cluster_outbox").fetchone()[0], 1)

    def test_lost_mark_after_journal_append_is_retried_without_duplicate(self):
        connection = self.apps[0]
        connection.execute("BEGIN IMMEDIATE")
        enqueue(connection, "retry-key", "message.create", {"id": 7, "body": "retry"})
        connection.execute("COMMIT")
        event, duplicate = self.journals[0].append("retry-key", "message.create", {"id": 7, "body": "retry"})
        self.assertFalse(duplicate)
        self.assertEqual(drain_outbox(connection, self.journals[0]), 1)
        self.assertEqual(len(self.journals[0].operations()), 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM cluster_applied").fetchone()[0], 1)
        self.assertEqual(drain_outbox(connection, self.journals[0]), 0)
        self.assertEqual(connection.execute("SELECT operation_id FROM cluster_applied").fetchone()[0],
                         event["operation_id"])

    def test_remote_operation_applies_exactly_once_and_dependency_can_defer(self):
        source, target = self.apps
        target.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,body TEXT NOT NULL)")
        source.execute("BEGIN IMMEDIATE")
        enqueue(source, "remote-key", "message.create", {"id": 42, "body": "remote"})
        source.execute("COMMIT")
        drain_outbox(source, self.journals[0])
        self.journals[1].ingest(self.journals[0].export("node-a"))
        operations = self.journals[1].operations()
        waits = [True, False]

        def handler(connection, payload):
            if waits.pop(0):
                raise DeferredOperation()
            connection.execute("INSERT INTO messages VALUES(?,?)", (payload["id"], payload["body"]))

        self.assertEqual(apply_operations(target, operations, {"message.create": handler}),
                         {"applied": 0, "deferred": 1})
        self.assertEqual(apply_operations(target, operations, {"message.create": handler}),
                         {"applied": 1, "deferred": 0})
        self.assertEqual(apply_operations(target, operations, {"message.create": handler}),
                         {"applied": 0, "deferred": 0})
        self.assertEqual(target.execute("SELECT * FROM messages").fetchall(), [(42, "remote")])

    def test_conflicting_outbox_retry_is_rejected_without_rewriting_payload(self):
        connection = self.apps[0]
        connection.execute("BEGIN IMMEDIATE")
        enqueue(connection, "same-key", "message.create", {"body": "first"})
        connection.execute("COMMIT")
        connection.execute("BEGIN IMMEDIATE")
        try:
            self.error(409, enqueue, connection, "same-key", "message.create", {"body": "changed"})
        finally:
            connection.execute("ROLLBACK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
