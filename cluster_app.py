"""Application-side durability primitives for the distributed telegent work.

The module allocates collision-free SQLite integer ids and bridges one local
application transaction to the peer journal through a durable outbox.  It does
not know server.py's schema or apply message payloads by itself.
"""
import hashlib
import json
import re
import sqlite3
import time

from cluster_peer import PeerError, canonical_payload, logical_operation_id, operation_mode


# 41 timestamp bits + 8 stable node-slot bits + 4 sequence bits = 53 bits.
# The epoch keeps generated ids above all legacy autoincrement ids.
ID_EPOCH_MS = 1704067200000  # 2024-01-01 UTC
MAX_TIME_PART = (1 << 41) - 1
MAX_SAFE_INTEGER = (1 << 53) - 1
REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


SCHEMA = """
CREATE TABLE IF NOT EXISTS cluster_id_state(
 id INTEGER PRIMARY KEY CHECK(id=1), node_slot INTEGER NOT NULL,
 last_ms INTEGER NOT NULL, sequence INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cluster_outbox(
 request_key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_hash TEXT NOT NULL,
 payload BLOB NOT NULL, created REAL NOT NULL, exported REAL);
CREATE INDEX IF NOT EXISTS cluster_outbox_pending ON cluster_outbox(exported,created);
CREATE TABLE IF NOT EXISTS cluster_applied(
 operation_id TEXT PRIMARY KEY, applied REAL NOT NULL);
"""


class DeferredOperation(Exception):
    """A verified remote operation waits for a dependency such as its file."""


def install(connection, node_slot):
    if type(node_slot) is not int or not 0 <= node_slot <= 255:
        raise PeerError(400, "номер серверного узла должен быть от 0 до 255")
    connection.executescript(SCHEMA)
    row = connection.execute("SELECT node_slot FROM cluster_id_state WHERE id=1").fetchone()
    if row is None:
        connection.execute("INSERT INTO cluster_id_state VALUES(1,?,-1,-1)", (node_slot,))
    elif row[0] != node_slot:
        raise PeerError(409, "номер узла не совпадает с сохранённым генератором id")


def next_id(connection, node_slot, now_ms=None):
    """Allocate a JS-safe id inside the caller's current application transaction."""
    if not connection.in_transaction:
        raise PeerError(500, "id распределённой записи выделяется только внутри транзакции")
    row = connection.execute(
        "SELECT node_slot,last_ms,sequence FROM cluster_id_state WHERE id=1").fetchone()
    if row is None or row[0] != node_slot:
        raise PeerError(409, "генератор id узла не настроен")
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if type(now_ms) is not int:
        raise PeerError(400, "некорректное время генератора id")
    relative = now_ms - ID_EPOCH_MS
    if relative < 0:
        relative = 0
    logical = max(relative, row[1])
    sequence = row[2] + 1 if logical == row[1] else 0
    if sequence > 15:
        logical += 1
        sequence = 0
    if logical > MAX_TIME_PART:
        raise PeerError(507, "исчерпан диапазон распределённых id")
    value = (logical << 12) | (node_slot << 4) | sequence
    if value <= 0 or value > MAX_SAFE_INTEGER:
        raise PeerError(507, "распределённый id вышел за безопасный диапазон")
    connection.execute("UPDATE cluster_id_state SET last_ms=?,sequence=? WHERE id=1",
                       (logical, sequence))
    return value


def enqueue(connection, request_key, kind, payload, created=None):
    """Add one event to the outbox in the caller's application transaction."""
    if not connection.in_transaction:
        raise PeerError(500, "событие кластера ставится в outbox только внутри транзакции")
    if not isinstance(request_key, str) or REQUEST_KEY_RE.fullmatch(request_key) is None:
        raise PeerError(400, "некорректный ключ повтора outbox")
    if operation_mode(kind) != "mergeable":
        raise PeerError(425, "операция требует кворумного журнала")
    raw = canonical_payload(payload)
    digest = hashlib.sha256(raw).hexdigest()
    row = connection.execute(
        "SELECT kind,payload_hash FROM cluster_outbox WHERE request_key=?", (request_key,)).fetchone()
    if row is not None:
        if (row[0], row[1]) != (kind, digest):
            raise PeerError(409, "ключ повтора outbox уже использован с другим содержимым")
        return True
    connection.execute("INSERT INTO cluster_outbox VALUES(?,?,?,?,?,NULL)",
                       (request_key, kind, digest, raw, time.time() if created is None else created))
    return False


def drain_outbox(connection, journal, limit=100):
    """Idempotently append committed local outbox rows to the peer journal."""
    if connection.in_transaction:
        raise PeerError(500, "outbox выгружается вне транзакции приложения")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise PeerError(400, "некорректный размер выгрузки outbox")
    rows = connection.execute("SELECT request_key,kind,payload FROM cluster_outbox "
                              "WHERE exported IS NULL ORDER BY created,request_key LIMIT ?", (limit,)).fetchall()
    exported = 0
    for request_key, kind, raw in rows:
        try:
            payload = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise PeerError(500, "локальный outbox повреждён")
        event, _ = journal.append(request_key, kind, payload)
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = connection.execute(
                "SELECT kind,payload_hash FROM cluster_outbox WHERE request_key=?", (request_key,)).fetchone()
            if current is None or tuple(current) != (kind, event["payload_hash"]):
                raise PeerError(409, "outbox изменился во время выгрузки")
            connection.execute("UPDATE cluster_outbox SET exported=? WHERE request_key=?",
                               (time.time(), request_key))
            # The originating application transaction already materialized this event.
            connection.execute("INSERT OR IGNORE INTO cluster_applied VALUES(?,?)",
                               (event["operation_id"], time.time()))
            connection.execute("COMMIT")
            exported += 1
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    return exported


def apply_operations(connection, operations, handlers, limit=100):
    """Apply verified remote operations exactly once through kind-specific handlers."""
    if connection.in_transaction:
        raise PeerError(500, "операции кластера применяются вне другой транзакции")
    if (not isinstance(operations, list) or not isinstance(handlers, dict) or
            type(limit) is not int or not 1 <= limit <= 1000):
        raise PeerError(400, "некорректный пакет операций приложения")
    applied = 0
    deferred = 0
    for event in operations[:limit]:
        if not isinstance(event, dict):
            raise PeerError(400, "некорректная операция приложения")
        operation_id = event.get("operation_id")
        kind = event.get("kind")
        payload = event.get("payload")
        request_key = event.get("request_key")
        if (not isinstance(operation_id, str) or len(operation_id) != 64 or
                logical_operation_id(event.get("cluster"), request_key, kind, payload) != operation_id):
            raise PeerError(409, "операция приложения не прошла проверку id")
        if connection.execute("SELECT 1 FROM cluster_applied WHERE operation_id=?", (operation_id,)).fetchone():
            continue
        handler = handlers.get(kind)
        if handler is None:
            raise PeerError(425, "для операции приложения ещё нет обработчика")
        connection.execute("BEGIN IMMEDIATE")
        try:
            if connection.execute("SELECT 1 FROM cluster_applied WHERE operation_id=?", (operation_id,)).fetchone():
                connection.execute("COMMIT")
                continue
            handler(connection, payload)
            connection.execute("INSERT INTO cluster_applied VALUES(?,?)", (operation_id, time.time()))
            connection.execute("COMMIT")
            applied += 1
        except DeferredOperation:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            deferred += 1
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    return {"applied": applied, "deferred": deferred}
