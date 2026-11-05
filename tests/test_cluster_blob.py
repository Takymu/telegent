"""Immutable blob storage verifies bytes before publication."""
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cluster_blob import BlobStore, hash_file
from cluster_peer import PeerError
from cluster_wire import sign_blob_response, verify_blob_response


KEY = b"blob-pair-key-" + b"x" * 48


class BrokenStream:
    def __init__(self, data, stop):
        self.data = io.BytesIO(data[:stop])

    def read(self, size=-1):
        return self.data.read(size)


class ClusterBlobTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="telegent-blob-")
        self.root = Path(self.temp.name)
        self.store = BlobStore(str(self.root / "store"), reserve_bytes=0)
        self.data = (b"distributed blob fixture\n" * 50000) + b"end"
        import hashlib
        self.sha = hashlib.sha256(self.data).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(PeerError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def test_receive_stream_publishes_only_verified_complete_blob(self):
        path = self.store.receive(io.BytesIO(self.data), self.sha, len(self.data))
        self.assertEqual(hash_file(path), (self.sha, len(self.data)))
        self.assertTrue(self.store.has(self.sha, len(self.data)))
        self.assertEqual(self.store.receive(io.BytesIO(self.data), self.sha, len(self.data)), path)

    def test_interrupted_wrong_hash_and_extra_byte_leave_no_published_blob(self):
        other_root = self.root / "other"
        store = BlobStore(str(other_root), reserve_bytes=0)
        self.error(503, store.receive, BrokenStream(self.data, len(self.data) // 2), self.sha, len(self.data))
        self.assertFalse(os.path.exists(store.path(self.sha)))
        self.error(409, store.receive, io.BytesIO(self.data + b"x"), self.sha, len(self.data))
        self.assertFalse(os.path.exists(store.path(self.sha)))
        self.error(409, store.receive, io.BytesIO(self.data), "f" * 64, len(self.data))
        self.assertFalse(os.path.exists(store.path("f" * 64)))

    def test_adopt_and_materialize_prefer_hardlinks_without_changing_bytes(self):
        source = self.root / "upload"
        source.write_bytes(self.data)
        stored = self.store.adopt(str(source), self.sha, len(self.data))
        destination = self.root / "uploads" / ("u_" + "a" * 48)
        self.store.materialize(self.sha, len(self.data), str(destination))
        self.assertEqual(destination.read_bytes(), self.data)
        if os.name != "nt":
            self.assertEqual(os.stat(source).st_ino, os.stat(stored).st_ino)
            self.assertEqual(os.stat(stored).st_ino, os.stat(destination).st_ino)

    def test_existing_wrong_destination_and_source_manifest_are_rejected(self):
        source = self.root / "upload"
        source.write_bytes(self.data)
        self.error(409, self.store.adopt, str(source), "e" * 64, len(self.data))
        self.store.adopt(str(source), self.sha, len(self.data))
        destination = self.root / "occupied"
        destination.write_bytes(b"different")
        self.error(409, self.store.materialize, self.sha, len(self.data), str(destination))

    def test_reserve_is_checked_before_copy_or_receive(self):
        store = BlobStore(str(self.root / "reserved"), reserve_bytes=1000)
        fake = type("Usage", (), {"total": 1000, "used": 999, "free": 1})()
        with mock.patch("cluster_blob.shutil.disk_usage", return_value=fake):
            self.error(507, store.receive, io.BytesIO(self.data), self.sha, len(self.data))
        self.assertFalse(os.path.exists(store.path(self.sha)))

    def test_blob_response_signature_binds_manifest_and_request_nonce(self):
        headers = sign_blob_response(KEY, "node-b", "node-a", "nonce-a", self.sha, len(self.data))
        self.assertEqual(verify_blob_response(KEY, "node-b", "node-a", "nonce-a", headers),
                         {"sha256": self.sha, "size": len(self.data)})
        changed = dict(headers, **{"X-Telegent-Blob-Size": str(len(self.data) + 1)})
        self.error(401, verify_blob_response, KEY, "node-b", "node-a", "nonce-a", changed)
        self.error(401, verify_blob_response, KEY, "node-b", "node-a", "nonce-b", headers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
