"""Durable crash-fault quorum log for trust-changing cluster operations.

Each phone is an equal proposer, acceptor and learner.  The core implements the
persistent acceptor rules of single-decree Paxos for consecutive log slots.  A
network adapter must authenticate every response before passing it to
``choose_value`` or ``commit``; receipts are intentionally not public-key proofs.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading

from cluster_peer import GENESIS, PeerError, quorum_size


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_VALUE_BYTES = 8 * 1024 * 1024


def _identifier(value):
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def _canonical(value):
    def check(item, active):
        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise PeerError(400, "значение кворума содержит недопустимое число")
            return
        marker = id(item)
        if marker in active:
            raise PeerError(400, "значение кворума содержит цикл")
        active.add(marker)
        try:
            if isinstance(item, list):
                for child in item:
                    check(child, active)
                return
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise PeerError(400, "ключи значения кворума должны быть строками")
                    check(child, active)
                return
        finally:
            active.remove(marker)
        raise PeerError(400, "значение кворума содержит неподдерживаемый тип")

    check(value, set())
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PeerError(400, "значение кворума нельзя сериализовать")
    if len(raw) > MAX_VALUE_BYTES:
        raise PeerError(413, "значение кворума слишком большое")
    return raw


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _ballot(counter, node):
    if type(counter) is not int or counter < 0 or not _identifier(node):
        raise PeerError(400, "некорректный номер предложения")
    return counter, node


def _commit_hash(slot, previous_hash, value_hash):
    return _hash(json.dumps([slot, previous_hash, value_hash], separators=(",", ":")).encode("utf-8"))


def _open(path):
    if os.path.islink(path):
        raise PeerError(500, "не удалось безопасно открыть кворумный журнал")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise PeerError(500, "не удалось безопасно открыть кворумный журнал") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PeerError(500, "кворумный журнал должен быть обычным файлом")
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
    finally:
        os.close(descriptor)
    return sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)


class QuorumLog:
    def __init__(self, path, cluster, node, members):
        members = tuple(sorted(set(members)))
        if (not _identifier(cluster) or not _identifier(node) or node not in members or len(members) < 2 or
                any(not _identifier(member) for member in members)):
            raise PeerError(400, "некорректная личность кворумного узла")
        self.path = os.path.abspath(path)
        self.cluster = cluster
        self.node = node
        self.members = members
        self.lock = threading.RLock()
        self.db = _open(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
CREATE TABLE IF NOT EXISTS quorum_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quorum_slots(
 slot INTEGER PRIMARY KEY, previous_hash TEXT NOT NULL,
 promised_counter INTEGER NOT NULL, promised_node TEXT NOT NULL,
 accepted_counter INTEGER, accepted_node TEXT, value_hash TEXT, value BLOB,
 committed INTEGER NOT NULL DEFAULT 0, commit_hash TEXT, certificate BLOB);
""")
        if "certificate" not in {row[1] for row in self.db.execute("PRAGMA table_info(quorum_slots)")}:
            self.db.execute("ALTER TABLE quorum_slots ADD COLUMN certificate BLOB")
        try:
            with self.transaction():
                expected = {"cluster": cluster, "node": node, "members": json.dumps(members),
                            "head": "0", "head_hash": GENESIS, "proposal_counter": "0"}
                current = {row["key"]: row["value"] for row in self.db.execute("SELECT * FROM quorum_meta")}
                if current and any(current.get(key) != value for key, value in expected.items()
                                   if key not in ("head", "head_hash")):
                    raise PeerError(409, "кворумный журнал принадлежит другой конфигурации")
                if not current:
                    self.db.executemany("INSERT INTO quorum_meta VALUES(?,?)", expected.items())
                else:
                    self.db.execute("INSERT OR IGNORE INTO quorum_meta VALUES('proposal_counter','0')")
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

    def _meta(self):
        rows = {row["key"]: row["value"] for row in self.db.execute("SELECT * FROM quorum_meta")}
        return int(rows["head"]), rows["head_hash"]

    def state(self):
        with self.transaction():
            head, head_hash = self._meta()
            return {"cluster": self.cluster, "node": self.node, "members": self.members,
                    "head": head, "head_hash": head_hash}

    def next_ballot(self, minimum=0):
        if type(minimum) is not int or minimum < 0:
            raise PeerError(400, "некорректная нижняя граница ballot")
        with self.transaction():
            current = int(self.db.execute(
                "SELECT value FROM quorum_meta WHERE key='proposal_counter'").fetchone()[0])
            counter = max(current + 1, minimum)
            self.db.execute("UPDATE quorum_meta SET value=? WHERE key='proposal_counter'", (str(counter),))
            return counter

    def _slot(self, slot, previous_hash):
        if (type(slot) is not int or slot < 1 or not isinstance(previous_hash, str) or
                len(previous_hash) != 64 or any(character not in "0123456789abcdef" for character in previous_hash)):
            raise PeerError(400, "некорректная позиция кворумного журнала")
        head, head_hash = self._meta()
        if slot != head + 1 or previous_hash != head_hash:
            raise PeerError(409, "узел должен сначала догнать кворумный журнал")
        row = self.db.execute("SELECT * FROM quorum_slots WHERE slot=?", (slot,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO quorum_slots(slot,previous_hash,promised_counter,promised_node) "
                            "VALUES(?,?,-1,'')", (slot, previous_hash))
            row = self.db.execute("SELECT * FROM quorum_slots WHERE slot=?", (slot,)).fetchone()
        elif row["previous_hash"] != previous_hash:
            raise PeerError(409, "предыдущая запись кворума не совпадает")
        return row

    def prepare(self, slot, previous_hash, counter, proposer):
        ballot = _ballot(counter, proposer)
        if proposer not in self.members:
            raise PeerError(403, "предложение пришло от неизвестного узла")
        with self.transaction():
            row = self._slot(slot, previous_hash)
            promised = row["promised_counter"], row["promised_node"]
            if ballot < promised:
                raise PeerError(409, "узел уже обещал более новое предложение")
            if ballot > promised:
                self.db.execute("UPDATE quorum_slots SET promised_counter=?,promised_node=? WHERE slot=?",
                                (counter, proposer, slot))
            accepted = None
            if row["accepted_counter"] is not None:
                accepted = {"counter": row["accepted_counter"], "node": row["accepted_node"],
                            "value_hash": row["value_hash"],
                            "value": json.loads(bytes(row["value"]).decode("utf-8"))}
            return {"node": self.node, "slot": slot, "previous_hash": previous_hash,
                    "ballot": {"counter": counter, "node": proposer}, "accepted": accepted}

    def accept(self, slot, previous_hash, counter, proposer, value):
        ballot = _ballot(counter, proposer)
        if proposer not in self.members:
            raise PeerError(403, "предложение пришло от неизвестного узла")
        raw = _canonical(value)
        value_hash = _hash(raw)
        with self.transaction():
            row = self._slot(slot, previous_hash)
            promised = row["promised_counter"], row["promised_node"]
            if ballot < promised:
                raise PeerError(409, "узел уже обещал более новое предложение")
            accepted = (row["accepted_counter"], row["accepted_node"]) if row["accepted_counter"] is not None else None
            if accepted == ballot and row["value_hash"] != value_hash:
                raise PeerError(409, "одно предложение не может содержать два значения")
            self.db.execute("""UPDATE quorum_slots SET promised_counter=?,promised_node=?,
                accepted_counter=?,accepted_node=?,value_hash=?,value=? WHERE slot=?""",
                (counter, proposer, counter, proposer, value_hash, raw, slot))
            return {"node": self.node, "slot": slot, "previous_hash": previous_hash,
                    "ballot": {"counter": counter, "node": proposer}, "value_hash": value_hash}

    def commit(self, slot, previous_hash, value, receipts):
        raw = _canonical(value)
        value_hash = _hash(raw)
        if not isinstance(receipts, list):
            raise PeerError(400, "нужны квитанции кворума")
        voters = set()
        ballot = None
        for receipt in receipts:
            if not isinstance(receipt, dict) or set(receipt) != {
                    "node", "slot", "previous_hash", "ballot", "value_hash"}:
                raise PeerError(400, "некорректная квитанция кворума")
            current_ballot = receipt["ballot"]
            if (receipt["node"] not in self.members or receipt["slot"] != slot or
                    receipt["previous_hash"] != previous_hash or receipt["value_hash"] != value_hash or
                    not isinstance(current_ballot, dict) or set(current_ballot) != {"counter", "node"}):
                raise PeerError(409, "квитанции кворума не подтверждают одно значение")
            parsed_ballot = _ballot(current_ballot["counter"], current_ballot["node"])
            if parsed_ballot[1] not in self.members:
                raise PeerError(409, "квитанции кворума относятся к неизвестному proposer")
            if ballot is None:
                ballot = parsed_ballot
            if parsed_ballot != ballot or receipt["node"] in voters:
                raise PeerError(409, "квитанции кворума не подтверждают одно предложение")
            voters.add(receipt["node"])
        if len(voters) < quorum_size(len(self.members)):
            raise PeerError(425, "для операции не собрано большинство серверов")
        certificate = sorted(receipts, key=lambda receipt: receipt["node"])
        certificate_raw = _canonical(certificate)
        commit_hash = _commit_hash(slot, previous_hash, value_hash)
        with self.transaction():
            head, head_hash = self._meta()
            if slot <= head:
                row = self.db.execute("SELECT * FROM quorum_slots WHERE slot=?", (slot,)).fetchone()
                if row is not None and row["committed"] and row["commit_hash"] == commit_hash:
                    return {"slot": slot, "hash": commit_hash, "duplicate": True}
                raise PeerError(409, "кворумный журнал уже содержит другое значение")
            row = self._slot(slot, previous_hash)
            if row["committed"] and row["commit_hash"] != commit_hash:
                raise PeerError(409, "кворумный журнал уже содержит другое значение")
            self.db.execute("""UPDATE quorum_slots SET accepted_counter=?,accepted_node=?,value_hash=?,value=?,
                committed=1,commit_hash=?,certificate=? WHERE slot=?""",
                (ballot[0], ballot[1], value_hash, raw, commit_hash, certificate_raw, slot))
            self.db.execute("UPDATE quorum_meta SET value=? WHERE key='head'", (str(slot),))
            self.db.execute("UPDATE quorum_meta SET value=? WHERE key='head_hash'", (commit_hash,))
        return {"slot": slot, "hash": commit_hash, "duplicate": False}

    def entries(self, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise PeerError(400, "некорректная позиция кворумного журнала")
        with self.transaction():
            return [{"slot": row["slot"], "previous_hash": row["previous_hash"],
                     "hash": row["commit_hash"], "value_hash": row["value_hash"],
                     "value": json.loads(bytes(row["value"]).decode("utf-8")),
                     "receipts": json.loads(bytes(row["certificate"]).decode("utf-8"))}
                    for row in self.db.execute("SELECT * FROM quorum_slots WHERE committed=1 AND slot>? "
                                               "ORDER BY slot LIMIT ?", (after, limit))]

    def learn(self, entry):
        if not isinstance(entry, dict) or set(entry) != {
                "slot", "previous_hash", "hash", "value_hash", "value", "receipts"}:
            raise PeerError(400, "некорректная committed-запись")
        raw = _canonical(entry["value"])
        if (_hash(raw) != entry["value_hash"] or
                _commit_hash(entry["slot"], entry["previous_hash"], entry["value_hash"]) != entry["hash"]):
            raise PeerError(409, "committed-запись повреждена")
        result = self.commit(entry["slot"], entry["previous_hash"], entry["value"], entry["receipts"])
        if result["hash"] != entry["hash"]:
            raise PeerError(409, "цепочка committed-записей расходится")
        return result


def choose_value(responses, members, proposed):
    """Apply Paxos phase-one choice after responses were authenticated by transport."""
    members = tuple(sorted(set(members)))
    if not isinstance(responses, list):
        raise PeerError(400, "нужны ответы подготовки кворума")
    nodes = set()
    ballot = None
    slot = None
    previous_hash = None
    accepted = []
    for response in responses:
        if not isinstance(response, dict) or set(response) != {
                "node", "slot", "previous_hash", "ballot", "accepted"}:
            raise PeerError(400, "некорректный ответ подготовки кворума")
        if response["node"] not in members or response["node"] in nodes:
            raise PeerError(409, "ответы подготовки не образуют один кворум")
        nodes.add(response["node"])
        current = response["ballot"]
        if not isinstance(current, dict) or set(current) != {"counter", "node"}:
            raise PeerError(400, "некорректный номер предложения")
        current_ballot = _ballot(current["counter"], current["node"])
        if ballot is None:
            ballot, slot, previous_hash = current_ballot, response["slot"], response["previous_hash"]
        if (current_ballot, response["slot"], response["previous_hash"]) != (ballot, slot, previous_hash):
            raise PeerError(409, "ответы подготовки относятся к разным предложениям")
        if response["accepted"] is not None:
            item = response["accepted"]
            if not isinstance(item, dict) or set(item) != {"counter", "node", "value_hash", "value"}:
                raise PeerError(400, "некорректное принятое значение")
            accepted_ballot = _ballot(item["counter"], item["node"])
            if accepted_ballot[1] not in members:
                raise PeerError(409, "принятое значение относится к неизвестному proposer")
            accepted.append((accepted_ballot, item))
    if len(nodes) < quorum_size(len(members)):
        raise PeerError(425, "для операции не собрано большинство серверов")
    if accepted:
        highest = max(pair[0] for pair in accepted)
        candidates = [item for ballot, item in accepted if ballot == highest]
        if len({item["value_hash"] for item in candidates}) != 1:
            raise PeerError(409, "узлы сообщили разные значения одного предложения")
        item = candidates[0]
        if _hash(_canonical(item["value"])) != item["value_hash"]:
            raise PeerError(409, "принятое значение повреждено")
        return item["value"]
    _canonical(proposed)
    return proposed
