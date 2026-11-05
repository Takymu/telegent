"""Message-create codec preserves scopes and defers unavailable dependencies."""
import hashlib
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cluster_app import DeferredOperation
from cluster_message import apply_message, encode_message
from cluster_peer import PeerError


MESSAGE_SCHEMA = """
CREATE TABLE users(name TEXT PRIMARY KEY,kind TEXT,owner TEXT,revoked INTEGER DEFAULT 0);
CREATE TABLE projects(id INTEGER PRIMARY KEY,title TEXT);
CREATE TABLE members(project_id INTEGER,user TEXT,role TEXT,PRIMARY KEY(project_id,user));
CREATE TABLE conversations(id TEXT PRIMARY KEY,project_id INTEGER,human TEXT,agent TEXT);
CREATE TABLE messages(
 id INTEGER PRIMARY KEY,ts REAL,author TEXT,author_kind TEXT,owner TEXT,host TEXT,recipients TEXT,
 topic TEXT,type TEXT,priority TEXT,subject TEXT,body TEXT,reply_to INTEGER,thread_id INTEGER,
 needs_approval INTEGER,status TEXT,status_by TEXT,status_ts REAL,status_ref TEXT,status_note TEXT,
 result TEXT,idem_key TEXT,approver TEXT,revision INTEGER,mentions_json TEXT,edited_ts REAL,
 deleted_ts REAL,conversation_id TEXT,project_id INTEGER);
CREATE TABLE attachments(
 id INTEGER PRIMARY KEY,message_id INTEGER,name TEXT,size INTEGER,md5 TEXT,link TEXT,content BLOB,upload_id TEXT);
CREATE TABLE file_uploads(
 id TEXT PRIMARY KEY,project_id INTEGER,user TEXT,name TEXT,size INTEGER,received INTEGER,md5 TEXT,
 expected_md5 TEXT,idem_key TEXT,created REAL,updated REAL,attachment_id INTEGER);
CREATE TABLE secret_envelopes(message_id INTEGER PRIMARY KEY);
"""


class ClusterMessageTest(unittest.TestCase):
    def connection(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.executescript(MESSAGE_SCHEMA)
        connection.execute("INSERT INTO users VALUES('account-a','human','account-a',0)")
        connection.execute("INSERT INTO projects VALUES(7,'fixture')")
        connection.execute("INSERT INTO members VALUES(7,'account-a','member')")
        return connection

    def insert_message(self, connection, message_id=500000000000001, reply_to=None):
        thread_id = message_id if reply_to is None else reply_to
        connection.execute("""INSERT INTO messages VALUES(
            ?,2000000000,'account-a','human','account-a','web',',account-b,','topic','info','normal','',
            'hello',?,?,0,NULL,NULL,NULL,NULL,NULL,NULL,'request-key',NULL,0,'[]',NULL,NULL,NULL,7)""",
                           (message_id, reply_to, thread_id))

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(PeerError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def test_plain_message_round_trips_with_same_global_id_and_scope(self):
        source, target = self.connection(), self.connection()
        try:
            self.insert_message(source)
            payload = encode_message(source, 500000000000001)
            target.execute("BEGIN IMMEDIATE")
            apply_message(target, payload)
            target.execute("COMMIT")
            row = target.execute("SELECT * FROM messages").fetchone()
            self.assertEqual((row["id"], row["project_id"], row["body"]),
                             (500000000000001, 7, "hello"))
        finally:
            source.close(); target.close()

    def test_inline_and_remote_file_manifests_round_trip_after_blob_is_ready(self):
        source, target = self.connection(), self.connection()
        try:
            self.insert_message(source)
            raw = b"inline fixture"
            source.execute("INSERT INTO attachments VALUES(500000000000101,500000000000001,'note.txt',?,?,NULL,?,NULL)",
                           (len(raw), hashlib.md5(raw).hexdigest(), raw))
            upload_id = "u_" + "a" * 48
            source.execute("INSERT INTO attachments VALUES(500000000000102,500000000000001,'large.bin',9,?,NULL,NULL,?)",
                           (hashlib.md5(b"123456789").hexdigest(), upload_id))
            payload = encode_message(source, 500000000000001,
                                     lambda item: {"sha256": hashlib.sha256(b"123456789").hexdigest(), "size": 9})
            target.execute("BEGIN IMMEDIATE")
            with self.assertRaises(DeferredOperation):
                apply_message(target, payload, lambda item: False)
            target.execute("ROLLBACK")
            target.execute("BEGIN IMMEDIATE")
            apply_message(target, payload, lambda item: item["sha256"] == hashlib.sha256(b"123456789").hexdigest())
            target.execute("COMMIT")
            rows = target.execute("SELECT * FROM attachments ORDER BY id").fetchall()
            self.assertEqual(bytes(rows[0]["content"]), raw)
            self.assertEqual(rows[1]["upload_id"], upload_id)
            self.assertEqual(target.execute("SELECT attachment_id FROM file_uploads").fetchone()[0],
                             500000000000102)
            target.execute("BEGIN IMMEDIATE")
            apply_message(target, payload, lambda item: True)
            target.execute("COMMIT")
            self.assertEqual(target.execute("SELECT COUNT(*) FROM attachments").fetchone()[0], 2)
        finally:
            source.close(); target.close()

    def test_reply_waits_for_parent_then_preserves_thread(self):
        source, target = self.connection(), self.connection()
        try:
            parent = 500000000000001
            child = 500000000000002
            self.insert_message(source, parent)
            self.insert_message(source, child, parent)
            payload = encode_message(source, child)
            target.execute("BEGIN IMMEDIATE")
            with self.assertRaises(DeferredOperation):
                apply_message(target, payload)
            target.execute("ROLLBACK")
            self.insert_message(target, parent)
            target.execute("BEGIN IMMEDIATE")
            apply_message(target, payload)
            target.execute("COMMIT")
            row = target.execute("SELECT reply_to,thread_id FROM messages WHERE id=?", (child,)).fetchone()
            self.assertEqual(tuple(row), (parent, parent))
        finally:
            source.close(); target.close()

    def test_missing_project_author_membership_or_conversation_defers(self):
        source = self.connection(); self.insert_message(source)
        payload = encode_message(source, 500000000000001)
        for statement in ("DELETE FROM projects", "DELETE FROM users", "DELETE FROM members"):
            target = self.connection(); target.execute(statement); target.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaises(DeferredOperation): apply_message(target, payload)
            finally:
                target.execute("ROLLBACK"); target.close()
        payload["message"]["conversation_id"] = "d_" + "b" * 48
        target = self.connection(); target.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(DeferredOperation): apply_message(target, payload)
        finally:
            target.execute("ROLLBACK"); target.close(); source.close()

    def test_secrets_edits_bad_checksums_and_id_collisions_are_rejected(self):
        source, target = self.connection(), self.connection()
        try:
            self.insert_message(source)
            source.execute("INSERT INTO secret_envelopes VALUES(500000000000001)")
            self.error(425, encode_message, source, 500000000000001)
            source.execute("DELETE FROM secret_envelopes")
            payload = encode_message(source, 500000000000001)
            payload["message"]["revision"] = 1
            self.error(409, apply_message, target, payload)
            payload["message"]["revision"] = 0
            self.insert_message(target)
            payload["message"]["body"] = "different"
            self.error(409, apply_message, target, payload)
        finally:
            source.close(); target.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
