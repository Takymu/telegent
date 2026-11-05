"""Durable coordinator core. Not integrated with the application or a hosted provider yet."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time


GENESIS = '0' * 64
MAX_ENTRY_BYTES = 8 * 1024 * 1024


class ControlError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9._-]{1,128}', value)) and value not in ('.', '..')


def _index(value):
    return type(value) is int and 0 <= value < 2**63


def _hash(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[0-9a-f]{64}', value))


def _token_hash(token):
    if not isinstance(token, str) or not 32 <= len(token) <= 256:
        raise ControlError(401, 'нужен отдельный ключ управления кластером')
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _payload_bytes(payload):
    pending,seen=[payload],set()
    while pending:
        item=pending.pop()
        if isinstance(item,(dict,list,tuple)) and id(item) not in seen:
            seen.add(id(item))
            if isinstance(item,dict):
                if any(not isinstance(key,str) for key in item):
                    raise ControlError(400,'ключи записи журнала должны быть строками')
                pending.extend(item.values())
            else:pending.extend(item)
    try: raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError): raise ControlError(400, 'некорректное содержимое записи журнала')
    if len(raw) > MAX_ENTRY_BYTES: raise ControlError(413, 'запись журнала слишком большая')
    return raw


def verify_entries(entries, base_index, base_hash):
    """A replica checks the chain and payload before applying or acknowledging a page."""
    if not _index(base_index) or not _hash(base_hash): raise ControlError(400, 'некорректная исходная позиция журнала')
    index, digest = base_index, base_hash
    for entry in entries:
        try:
            if (not _index(entry['idx']) or entry['idx'] != index+1 or entry['previous_hash'] != digest
                    or not _index(entry['epoch']) or not _identifier(entry['node'])
                    or not _identifier(entry['instance']) or not _identifier(entry['request_key'])
                    or not isinstance(entry['payload'],dict)):
                raise ValueError()
            payload_hash = hashlib.sha256(_payload_bytes(entry['payload'])).hexdigest()
            envelope = json.dumps([entry['idx'], entry['epoch'], entry['node'], entry['instance'],
                                  entry['request_key'], digest, payload_hash],separators=(',', ':')).encode('utf-8')
            calculated = hashlib.sha256(envelope).hexdigest()
            if payload_hash != entry['payload_hash'] or calculated != entry['entry_hash']: raise ValueError()
            index, digest = entry['idx'], calculated
        except (KeyError, ValueError, TypeError, ControlError):
            raise ControlError(409, 'нарушена целостность или последовательность журнала')
    return index, digest


class Coordinator:
    """One authoritative durable store, not a consensus implementation between witnesses.

    Every mutation is serialized by SQLite, including mutations from other processes.
    A hosting adapter must preserve these atomic operations and the journal. This class
    alone does not fence server.py, replicate its SQLite database or transfer attachments.
    """
    def __init__(self, path, clock=time.time, lease_seconds=20):
        if type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 120:
            raise ValueError('срок права записи должен быть от 5 до 120 секунд')
        self.clock, self.lease_seconds = clock, lease_seconds
        self.lock = threading.RLock()
        if path != ':memory:':
            # Restrict the database before WAL creation so new sidecars inherit its mode.
            fd=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('для журнала нужен обычный файл')
                os.chmod(path,0o600)
            finally:os.close(fd)
            for suffix in ('-wal','-shm'):
                sidecar=os.fspath(path)+suffix
                if os.path.islink(sidecar): raise ValueError('символическая ссылка вместо журнала запрещена')
                if os.path.exists(sidecar): os.chmod(sidecar,0o600)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.executescript('''
CREATE TABLE IF NOT EXISTS authority(
 id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL,
 owner TEXT, owner_instance TEXT, expires REAL NOT NULL,
 head INTEGER NOT NULL, head_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS principals(
 id TEXT PRIMARY KEY, role TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, instance TEXT NOT NULL DEFAULT '',
 ready INTEGER NOT NULL DEFAULT 0, seen REAL,
 applied INTEGER NOT NULL DEFAULT 0, applied_hash TEXT NOT NULL,
 free_bytes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS journal(
 idx INTEGER PRIMARY KEY, epoch INTEGER NOT NULL, node TEXT NOT NULL,
 instance TEXT NOT NULL, request_key TEXT UNIQUE NOT NULL,
 previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL,
 payload_hash TEXT NOT NULL, payload BLOB NOT NULL);
''')
        self.db.execute('INSERT OR IGNORE INTO authority VALUES(1,0,NULL,NULL,0,0,?)', (GENESIS,))

    def close(self):
        with self.lock: self.db.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction: self.db.execute('ROLLBACK')
                raise

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ControlError(503, 'координатор не может проверить своё время')
        return now

    def bootstrap(self, operator_token):
        digest = _token_hash(operator_token)
        with self.transaction():
            if self.db.execute('SELECT 1 FROM principals LIMIT 1').fetchone():
                raise ControlError(409, 'координатор уже настроен')
            self.db.execute("INSERT INTO principals(id,role,token_hash,applied_hash) VALUES('operator','operator',?,?)",
                            (digest, GENESIS))

    def _auth(self, token, role=None):
        row = self.db.execute('SELECT * FROM principals WHERE token_hash=? AND enabled=1', (_token_hash(token),)).fetchone()
        if row is None: raise ControlError(401, 'неизвестный или отозванный ключ управления')
        if role is not None and row['role'] != role:
            raise ControlError(403, 'это действие недоступно данной роли управления')
        return row

    def _node(self, token, instance):
        row = self._auth(token, 'node')
        if not _identifier(instance) or instance != row['instance']:
            raise ControlError(409, 'устаревший запуск узла; запись запрещена')
        return row

    def _state(self):
        return self.db.execute('SELECT * FROM authority WHERE id=1').fetchone()

    def add_node(self, operator_token, node, token):
        if not _identifier(node) or node == 'operator': raise ControlError(400, 'некорректный идентификатор узла')
        digest = _token_hash(token)
        with self.transaction():
            self._auth(operator_token, 'operator')
            if self.db.execute('SELECT 1 FROM principals WHERE id=? OR token_hash=?', (node, digest)).fetchone():
                raise ControlError(409, 'узел или ключ уже зарегистрирован')
            self.db.execute("INSERT INTO principals(id,role,token_hash,applied_hash) VALUES(?,'node',?,?)", (node, digest, GENESIS))

    def revoke(self, operator_token, node):
        with self.transaction():
            self._auth(operator_token, 'operator')
            row = self.db.execute("SELECT * FROM principals WHERE id=? AND role='node'", (node,)).fetchone()
            if row is None: raise ControlError(404, 'узел не найден')
            self.db.execute('UPDATE principals SET enabled=0,ready=0 WHERE id=?', (node,))
            if self._state()['owner'] == node:
                self.db.execute('UPDATE authority SET epoch=epoch+1,owner=NULL,owner_instance=NULL,expires=0 WHERE id=1')

    def open_session(self, token, instance, previous_instance=''):
        if not _identifier(instance): raise ControlError(400, 'нужен идентификатор запуска узла')
        with self.transaction():
            node = self._auth(token, 'node')
            if node['instance'] == instance: return
            if node['instance'] != previous_instance:
                raise ControlError(409, 'запуск узла уже изменился; старый запрос не принят')
            self.db.execute('UPDATE principals SET instance=?,ready=0,seen=NULL,applied=0,applied_hash=? WHERE id=?',
                            (instance, GENESIS, node['id']))
            if self._state()['owner'] == node['id']:
                self.db.execute('UPDATE authority SET epoch=epoch+1,owner=NULL,owner_instance=NULL,expires=0 WHERE id=1')

    def heartbeat(self, token, instance, ready, free_bytes):
        if type(ready) is not bool or not _index(free_bytes): raise ControlError(400, 'некорректное состояние узла')
        with self.transaction():
            node = self._node(token, instance)
            self.db.execute('UPDATE principals SET ready=?,free_bytes=?,seen=? WHERE id=?',
                            (int(ready), free_bytes, self._now(), node['id']))
            if not ready and (self._state()['owner'],self._state()['owner_instance']) == (node['id'],instance):
                self.db.execute('UPDATE authority SET epoch=epoch+1,owner=NULL,owner_instance=NULL,expires=0 WHERE id=1')

    def state(self, token):
        with self.transaction():
            self._auth(token)
            result = dict(self._state()); result.pop('id')
            result['nodes'] = [dict(row) for row in self.db.execute(
                "SELECT id,enabled,instance,ready,seen,applied,applied_hash,free_bytes FROM principals WHERE role='node' ORDER BY id")]
            return result

    def _entry_hash(self, index):
        if index == 0: return GENESIS
        row = self.db.execute('SELECT entry_hash FROM journal WHERE idx=?', (index,)).fetchone()
        if row is None: raise ControlError(409, 'неизвестная позиция журнала')
        return row['entry_hash']

    def acknowledge(self, token, instance, index, entry_hash):
        if not _index(index) or not _hash(entry_hash): raise ControlError(400, 'некорректная позиция журнала')
        with self.transaction():
            node = self._node(token, instance)
            if self._entry_hash(index) != entry_hash: raise ControlError(409, 'хеш журнала не совпадает')
            if index > node['applied']:
                self.db.execute('UPDATE principals SET applied=?,applied_hash=? WHERE id=?', (index, entry_hash, node['id']))

    def claim(self, token, instance):
        with self.transaction():
            node = self._node(token, instance); state = self._state(); now = self._now()
            if not node['ready'] or node['seen'] is None or not 0 <= now-node['seen'] <= self.lease_seconds:
                raise ControlError(425, 'узел ещё не подтвердил готовность')
            if (node['applied'], node['applied_hash']) != (state['head'], state['head_hash']):
                raise ControlError(425, 'сначала нужно восстановить подтверждённый журнал')
            ours = (state['owner'], state['owner_instance']) == (node['id'], instance)
            if state['expires'] > now and not ours: raise ControlError(409, 'право записи принадлежит другому узлу')
            epoch = state['epoch'] if ours and state['expires'] > now else state['epoch']+1
            self.db.execute('UPDATE authority SET owner=?,owner_instance=?,epoch=?,expires=? WHERE id=1',
                            (node['id'], instance, epoch, now+self.lease_seconds))
            return {'node':node['id'], 'instance':instance, 'epoch':epoch, 'expires':now+self.lease_seconds,
                    'head':state['head'], 'head_hash':state['head_hash']}

    def _owner(self, node, instance, epoch):
        state = self._state()
        if (state['owner'], state['owner_instance'], state['epoch']) != (node['id'], instance, epoch) or state['expires'] <= self._now():
            raise ControlError(409, 'право записи истекло или уже передано другому узлу')
        return state

    def append(self, token, instance, epoch, base_index, request_key, payload):
        if not _index(epoch) or not _index(base_index) or not _identifier(request_key) or not isinstance(payload, dict):
            raise ControlError(400, 'некорректная запись журнала')
        raw = _payload_bytes(payload)
        payload_hash = hashlib.sha256(raw).hexdigest()
        with self.transaction():
            node = self._node(token, instance)
            old = self.db.execute('SELECT * FROM journal WHERE request_key=?', (request_key,)).fetchone()
            if old:
                if old['payload_hash'] != payload_hash: raise ControlError(409, 'ключ повтора уже занят другой записью')
                return {'index':old['idx'], 'hash':old['entry_hash'], 'epoch':old['epoch'], 'duplicate':True}
            state = self._owner(node, instance, epoch)
            if state['head'] != base_index: raise ControlError(409, 'запись основана на устаревшем журнале')
            if (node['applied'],node['applied_hash']) != (state['head'],state['head_hash']):
                raise ControlError(425, 'сначала нужно применить предыдущую подтверждённую запись')
            index = state['head']+1
            envelope = json.dumps([index, epoch, node['id'], instance, request_key, state['head_hash'], payload_hash],
                                  separators=(',', ':')).encode('utf-8')
            digest = hashlib.sha256(envelope).hexdigest()
            self.db.execute('INSERT INTO journal VALUES(?,?,?,?,?,?,?,?,?)',
                            (index, epoch, node['id'], instance, request_key, state['head_hash'], digest, payload_hash, raw))
            self.db.execute('UPDATE authority SET head=?,head_hash=? WHERE id=1', (index, digest))
            return {'index':index, 'hash':digest, 'epoch':epoch, 'duplicate':False}

    def entries(self, token, instance, after, limit=100):
        if not _index(after) or type(limit) is not int or not 1 <= limit <= 100:
            raise ControlError(400, 'некорректная страница журнала')
        with self.transaction():
            self._node(token, instance)
            if after > self._state()['head']: raise ControlError(409, 'позиция впереди подтверждённого журнала')
            rows = self.db.execute('SELECT * FROM journal WHERE idx>? ORDER BY idx LIMIT ?', (after, limit))
            result, size = [], 0
            for row in rows:
                # A page is bounded by bytes as well as entry count on small devices.
                if result and size+len(row['payload']) > MAX_ENTRY_BYTES: break
                item = dict(row); item['payload'] = json.loads(item['payload']); result.append(item)
                size += len(row['payload'])
            return result
