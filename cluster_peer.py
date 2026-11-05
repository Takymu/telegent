"""Durable peer event journal for telegent's crash-fault cluster.

The journal has no distinguished server.  Every enrolled node owns one hash
chain and peers exchange those chains.  This module deliberately accepts only
operations that can be merged after a partition.  Identity, permission and
membership changes need the separate quorum path and are never smuggled into
the available path.

This is a storage/protocol core, not a network transport or server.py wiring.
"""
from contextlib import contextmanager
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading


GENESIS = "0" * 64
MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 16 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# These operations have stable identities or monotonic values and can be
# reconciled without deciding which partition was the leader.
MERGEABLE_KINDS = frozenset({
    "message.create",
    "attachment.manifest",
    "reaction.set",
    "receipt.advance",
    "progress.set",
})

# This list documents the minimum set which must never use the available path.
# Unknown kinds are conservative too: they require the future quorum path.
QUORUM_KINDS = frozenset({
    "cluster.membership",
    "account.create",
    "account.rename",
    "account.token_reset",
    "project.create",
    "project.rename",
    "project.membership",
    "project.permission",
    "invitation.create",
    "invitation.redeem",
    "message.edit",
    "message.delete",
    "secret.create",
    "secret.consume",
})


class PeerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _identifier(value):
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _validate_json(value, active=None):
    if value is None or type(value) in (bool, int, float, str):
        if type(value) is float and (value != value or value in (float("inf"), float("-inf"))):
            raise PeerError(400, "событие содержит недопустимое число")
        return
    if active is None:
        active = set()
    marker = id(value)
    if marker in active:
        raise PeerError(400, "событие содержит циклическую структуру")
    active.add(marker)
    try:
        if isinstance(value, list):
            for item in value:
                _validate_json(item, active)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise PeerError(400, "ключи события должны быть строками")
                _validate_json(item, active)
            return
    finally:
        active.remove(marker)
    raise PeerError(400, "событие содержит неподдерживаемое значение")


def _payload_bytes(payload):
    _validate_json(payload)
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PeerError(400, "событие нельзя сериализовать")
    if len(raw) > MAX_EVENT_BYTES:
        raise PeerError(413, "событие слишком большое")
    return raw


def _operation_id(cluster, request_key, kind, payload_hash):
    raw = json.dumps([cluster, request_key, kind, payload_hash], separators=(",", ":")).encode("utf-8")
    return _digest(raw)


def _event_hash(cluster, origin, sequence, previous_hash, operation_id):
    raw = json.dumps([cluster, origin, sequence, previous_hash, operation_id],
                     separators=(",", ":")).encode("utf-8")
    return _digest(raw)


def operation_mode(kind):
    if not isinstance(kind, str):
        raise PeerError(400, "некорректный тип события")
    return "mergeable" if kind in MERGEABLE_KINDS else "quorum"


def quorum_size(member_count):
    if type(member_count) is not int or member_count < 1:
        raise PeerError(400, "кластер должен содержать хотя бы один узел")
    return member_count // 2 + 1


def has_quorum(members, available):
    voters = set(members)
    if not voters or any(not _identifier(node) for node in voters):
        raise PeerError(400, "некорректный состав кластера")
    return len(voters.intersection(available)) >= quorum_size(len(voters))


def _open_database(path):
    if os.path.islink(path):
        raise PeerError(500, "не удалось безопасно открыть журнал узла")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise PeerError(500, "не удалось безопасно открыть журнал узла") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PeerError(500, "журнал узла должен быть обычным файлом")
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
    finally:
        os.close(descriptor)
    return sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)


class PeerJournal:
    """One node's durable, append-only contribution to a peer cluster."""

    def __init__(self, path, cluster, node, members):
        if not _identifier(cluster) or not _identifier(node):
            raise PeerError(400, "некорректная личность кластера или узла")
        members = tuple(sorted(set(members)))
        if node not in members or len(members) < 2 or any(not _identifier(item) for item in members):
            raise PeerError(400, "некорректный состав кластера")
        self.path = os.path.abspath(path)
        self.cluster = cluster
        self.node = node
        self.members = members
        self.lock = threading.RLock()
        self.db = _open_database(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
CREATE TABLE IF NOT EXISTS peer_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peer_tips(
 origin TEXT PRIMARY KEY, sequence INTEGER NOT NULL, event_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peer_events(
 origin TEXT NOT NULL, sequence INTEGER NOT NULL, previous_hash TEXT NOT NULL,
 event_hash TEXT NOT NULL, operation_id TEXT NOT NULL, request_key TEXT NOT NULL,
 kind TEXT NOT NULL, payload_hash TEXT NOT NULL, payload BLOB NOT NULL,
 PRIMARY KEY(origin,sequence), UNIQUE(origin,request_key));
CREATE INDEX IF NOT EXISTS peer_events_operation ON peer_events(operation_id);
CREATE INDEX IF NOT EXISTS peer_events_request ON peer_events(request_key);
CREATE TABLE IF NOT EXISTS peer_conflicts(
 request_key TEXT PRIMARY KEY, first_operation TEXT NOT NULL, second_operation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peer_nonces(
 source TEXT NOT NULL, nonce TEXT NOT NULL, observed_at INTEGER NOT NULL,
 PRIMARY KEY(source,nonce));
""")
        try:
            with self.transaction():
                expected = {"cluster": cluster, "node": node, "members": json.dumps(members)}
                current = {row["key"]: row["value"] for row in self.db.execute("SELECT * FROM peer_meta")}
                if current and current != expected:
                    raise PeerError(409, "журнал принадлежит другому узлу или составу кластера")
                if not current:
                    self.db.executemany("INSERT INTO peer_meta VALUES(?,?)", expected.items())
                    self.db.executemany("INSERT INTO peer_tips VALUES(?,0,?)",
                                        ((member, GENESIS) for member in members))
        except BaseException:
            self.db.close()
            raise

    def close(self):
        with self.lock:
            self.db.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    def _tip(self, origin):
        row = self.db.execute("SELECT sequence,event_hash FROM peer_tips WHERE origin=?", (origin,)).fetchone()
        if row is None:
            raise PeerError(403, "событие пришло от неизвестного узла")
        return row["sequence"], row["event_hash"]

    def _build(self, origin, sequence, previous_hash, request_key, kind, payload):
        if not _identifier(request_key):
            raise PeerError(400, "некорректный ключ повтора")
        if operation_mode(kind) != "mergeable":
            raise PeerError(425, "операция требует кворума и недоступна в изолированной части кластера")
        raw = _payload_bytes(payload)
        payload_hash = _digest(raw)
        operation_id = _operation_id(self.cluster, request_key, kind, payload_hash)
        result = {
            "cluster": self.cluster,
            "origin": origin,
            "sequence": sequence,
            "previous_hash": previous_hash,
            "operation_id": operation_id,
            "request_key": request_key,
            "kind": kind,
            "payload_hash": payload_hash,
            "payload": json.loads(raw.decode("utf-8")),
        }
        result["event_hash"] = _event_hash(self.cluster, origin, sequence, previous_hash, operation_id)
        return result, raw

    def _insert(self, event, raw):
        self.db.execute("""INSERT INTO peer_events
            (origin,sequence,previous_hash,event_hash,operation_id,request_key,kind,payload_hash,payload)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (event["origin"], event["sequence"], event["previous_hash"], event["event_hash"],
             event["operation_id"], event["request_key"], event["kind"], event["payload_hash"], raw))
        self.db.execute("UPDATE peer_tips SET sequence=?,event_hash=? WHERE origin=?",
                        (event["sequence"], event["event_hash"], event["origin"]))
        variants = list(self.db.execute(
            "SELECT DISTINCT operation_id FROM peer_events WHERE request_key=? ORDER BY operation_id",
            (event["request_key"],)))
        if len(variants) > 1:
            self.db.execute("INSERT OR REPLACE INTO peer_conflicts VALUES(?,?,?)",
                            (event["request_key"], variants[0][0], variants[-1][0]))

    def append(self, request_key, kind, payload):
        """Append a mergeable local event, idempotently for this origin."""
        with self.transaction():
            existing = self.db.execute(
                "SELECT * FROM peer_events WHERE origin=? AND request_key=?", (self.node, request_key)).fetchone()
            sequence, previous_hash = self._tip(self.node)
            event, raw = self._build(self.node, sequence + 1, previous_hash, request_key, kind, payload)
            if existing is not None:
                if existing["operation_id"] != event["operation_id"]:
                    raise PeerError(409, "ключ повтора уже использован с другим содержимым")
                return self._row_event(existing), True
            self._insert(event, raw)
            return event, False

    def ingest(self, events):
        """Persist a contiguous batch received through an authenticated peer channel."""
        if not isinstance(events, list):
            raise PeerError(400, "ожидался список событий")
        total = 0
        inserted = 0
        with self.transaction():
            for supplied in events:
                if not isinstance(supplied, dict):
                    raise PeerError(400, "некорректное событие узла")
                required = {"cluster", "origin", "sequence", "previous_hash", "event_hash",
                            "operation_id", "request_key", "kind", "payload_hash", "payload"}
                if set(supplied) != required:
                    raise PeerError(400, "некорректный набор полей события")
                origin = supplied["origin"]
                if supplied["cluster"] != self.cluster or origin not in self.members:
                    raise PeerError(403, "событие относится к другому кластеру или узлу")
                sequence = supplied["sequence"]
                if type(sequence) is not int or sequence < 1:
                    raise PeerError(400, "некорректный номер события")
                current_sequence, current_hash = self._tip(origin)
                if sequence <= current_sequence:
                    row = self.db.execute("SELECT * FROM peer_events WHERE origin=? AND sequence=?",
                                          (origin, sequence)).fetchone()
                    if row is None or self._row_event(row) != supplied:
                        raise PeerError(409, "обнаружено расхождение цепочки узла")
                    continue
                if sequence != current_sequence + 1 or supplied["previous_hash"] != current_hash:
                    raise PeerError(409, "сначала нужны пропущенные события узла")
                rebuilt, raw = self._build(origin, sequence, current_hash, supplied["request_key"],
                                           supplied["kind"], supplied["payload"])
                total += len(raw)
                if total > MAX_BATCH_BYTES:
                    raise PeerError(413, "пакет событий слишком большой")
                for field in ("event_hash", "operation_id", "payload_hash"):
                    if supplied[field] != rebuilt[field]:
                        raise PeerError(409, "контрольная сумма события не совпадает")
                self._insert(rebuilt, raw)
                inserted += 1
        return inserted

    def _row_event(self, row):
        return {
            "cluster": self.cluster,
            "origin": row["origin"],
            "sequence": row["sequence"],
            "previous_hash": row["previous_hash"],
            "event_hash": row["event_hash"],
            "operation_id": row["operation_id"],
            "request_key": row["request_key"],
            "kind": row["kind"],
            "payload_hash": row["payload_hash"],
            "payload": json.loads(bytes(row["payload"]).decode("utf-8")),
        }

    def export(self, origin, after=0, limit=100):
        if origin not in self.members or type(after) is not int or after < 0:
            raise PeerError(400, "некорректная позиция обмена")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise PeerError(400, "некорректный размер пакета")
        with self.transaction():
            return [self._row_event(row) for row in self.db.execute(
                "SELECT * FROM peer_events WHERE origin=? AND sequence>? ORDER BY sequence LIMIT ?",
                (origin, after, limit))]

    def frontier(self):
        with self.transaction():
            return {row["origin"]: {"sequence": row["sequence"], "hash": row["event_hash"]}
                    for row in self.db.execute("SELECT * FROM peer_tips ORDER BY origin")}

    def hash_at(self, origin, sequence):
        if origin not in self.members or type(sequence) is not int or sequence < 0:
            raise PeerError(400, "некорректная позиция цепочки")
        with self.transaction():
            if sequence == 0:
                return GENESIS
            row = self.db.execute("SELECT event_hash FROM peer_events WHERE origin=? AND sequence=?",
                                  (origin, sequence)).fetchone()
            if row is None:
                raise PeerError(409, "неизвестная позиция цепочки")
            return row["event_hash"]

    def accept_nonce(self, source, nonce, observed_at, oldest_allowed):
        """Persist an authenticated transport nonce before processing its request."""
        if source not in self.members or not _identifier(nonce):
            raise PeerError(400, "некорректная сетевая квитанция")
        if type(observed_at) is not int or type(oldest_allowed) is not int:
            raise PeerError(400, "некорректное время сетевой квитанции")
        with self.transaction():
            self.db.execute("DELETE FROM peer_nonces WHERE observed_at<?", (oldest_allowed,))
            try:
                self.db.execute("INSERT INTO peer_nonces VALUES(?,?,?)", (source, nonce, observed_at))
            except sqlite3.IntegrityError:
                raise PeerError(409, "сетевой запрос уже был обработан")

    def operations(self):
        """Return one deterministic copy of each conflict-free logical operation."""
        with self.transaction():
            rows = self.db.execute("""SELECT e.* FROM peer_events e
                JOIN (SELECT operation_id,MIN(origin || ':' || printf('%020d',sequence)) AS first_copy
                      FROM peer_events GROUP BY operation_id) chosen ON chosen.operation_id=e.operation_id
                LEFT JOIN peer_conflicts c ON c.request_key=e.request_key
                WHERE c.request_key IS NULL
                  AND (e.origin || ':' || printf('%020d',e.sequence))=chosen.first_copy
                ORDER BY e.operation_id""").fetchall()
            return [self._row_event(row) for row in rows]

    def conflicts(self):
        with self.transaction():
            return [dict(row) for row in self.db.execute(
                "SELECT request_key,first_operation,second_operation FROM peer_conflicts ORDER BY request_key")]
