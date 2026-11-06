"""Optional server-node runtime for authenticated peer journal pulls.

The runtime is disabled unless ``server.config.json`` contains a ``cluster``
object. It does not yet translate application database writes into peer events.
"""
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from cluster_blob import BlobStore, MAX_BLOB_BYTES, valid_sha256
from cluster_consensus import Consensus
from cluster_peer import MAX_BATCH_BYTES, PeerError, PeerJournal
from cluster_quorum import QuorumLog
from cluster_wire import (BLOB_PATH, QUORUM_PREFIX, SYNC_PATH, apply_sync_response, build_sync_response,
                          decode_sync_request, encode_sync_request, sign_request,
                          sign_blob_response, sign_response, verify_blob_response,
                          verify_request, verify_response)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _private_key(path):
    path = os.path.abspath(path)
    if os.path.islink(path):
        raise PeerError(500, "файл ключа связи не должен быть ссылкой")
    try:
        st = os.stat(path)
        if not os.path.isfile(path):
            raise OSError("not a regular file")
        if os.name != "nt" and st.st_mode & 0o077:
            raise PeerError(500, "файл ключа связи доступен другим пользователям")
        with open(path, "rb") as stream:
            key = stream.read(4097).strip()
    except PeerError:
        raise
    except OSError as error:
        raise PeerError(500, "не удалось прочитать ключ связи между узлами") from error
    if not 32 <= len(key) <= 4096:
        raise PeerError(500, "ключ связи между узлами настроен неверно")
    return key


def _relative(base, value):
    if not isinstance(value, str) or not value:
        raise PeerError(400, "в конфигурации кластера не хватает пути")
    return os.path.abspath(value if os.path.isabs(value) else os.path.join(base, value))


class ClusterNode:
    def __init__(self, settings, config_dir, clock=time.time):
        if not isinstance(settings, dict):
            raise PeerError(400, "cluster должен быть объектом")
        allowed = {"id", "node", "slot", "journal", "quorum", "blobs", "peers", "poll_seconds", "allow_http_localhost"}
        if set(settings) - allowed:
            raise PeerError(400, "неизвестное поле конфигурации кластера")
        cluster = settings.get("id")
        node = settings.get("node")
        slot = settings.get("slot")
        if type(slot) is not int or not 0 <= slot <= 255:
            raise PeerError(400, "номер серверного узла должен быть от 0 до 255")
        peers = settings.get("peers")
        if not isinstance(peers, list) or not peers:
            raise PeerError(400, "нужен список соседних узлов кластера")
        self.clock = clock
        self.node = node
        self.slot = slot
        self.slots = {node: slot}
        self.peers = {}
        for item in peers:
            if not isinstance(item, dict) or set(item) != {"id", "slot", "url", "key_file"}:
                raise PeerError(400, "некорректное описание соседнего узла")
            peer = item["id"]
            peer_slot = item["slot"]
            if (peer == node or peer in self.peers or type(peer_slot) is not int or
                    not 0 <= peer_slot <= 255 or peer_slot in self.slots.values()):
                raise PeerError(400, "узлы кластера не должны повторяться")
            parsed = urllib.parse.urlsplit(item["url"])
            local_http = (settings.get("allow_http_localhost") is True and parsed.scheme == "http" and
                          parsed.hostname in ("127.0.0.1", "::1", "localhost"))
            if ((parsed.scheme != "https" and not local_http) or not parsed.netloc or parsed.query or
                    parsed.fragment or parsed.path not in ("", "/")):
                raise PeerError(400, "адрес соседнего узла должен быть корнем HTTPS")
            key_path = _relative(config_dir, item["key_file"])
            self.peers[peer] = {"url": item["url"].rstrip("/"), "key": _private_key(key_path)}
            self.slots[peer] = peer_slot
        members = tuple(sorted((node,) + tuple(self.peers)))
        journal_path = _relative(config_dir, settings.get("journal"))
        self.journal = PeerJournal(journal_path, cluster, node, members)
        self.quorum = QuorumLog(_relative(config_dir, settings.get("quorum") or (journal_path + ".quorum")),
                                cluster, node, members)
        self.blobs = BlobStore(_relative(config_dir, settings.get("blobs") or (journal_path + ".blobs")))
        poll = settings.get("poll_seconds", 10)
        if type(poll) not in (int, float) or not 1 <= poll <= 3600:
            self.journal.close()
            raise PeerError(400, "интервал синхронизации должен быть от 1 до 3600 секунд")
        self.poll_seconds = float(poll)
        self.stop_event = threading.Event()
        self.thread = None
        self.status_lock = threading.Lock()
        self.status = {peer: {"last_success": None, "last_error": None, "failures": 0}
                       for peer in self.peers}

    def close(self):
        self.stop()
        self.quorum.close()
        self.journal.close()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="cluster-peer-sync-%s" % self.node)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=5)

    def handle_sync(self, body, headers):
        source = verify_request(self.journal, {peer: value["key"] for peer, value in self.peers.items()},
                                "POST", SYNC_PATH, body, headers, now=self.clock())
        frontier = decode_sync_request(body, self.journal.members)
        response = build_sync_response(self.journal, frontier)
        normalized = {str(name).lower(): str(value) for name, value in headers.items()}
        response_headers = sign_response(self.peers[source]["key"], self.node, source, SYNC_PATH,
                                         normalized["x-telegent-nonce"], response)
        return response, response_headers

    def handle_blob(self, body, headers):
        source = verify_request(self.journal, {peer: value["key"] for peer, value in self.peers.items()},
                                "POST", BLOB_PATH, body, headers, now=self.clock())
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise PeerError(400, "запрос blob повреждён")
        if not isinstance(request, dict) or set(request) != {"sha256"} or not valid_sha256(request["sha256"]):
            raise PeerError(400, "некорректный запрос blob")
        info = self.blobs.info(request["sha256"])
        normalized = {str(name).lower(): str(value) for name, value in headers.items()}
        response_headers = sign_blob_response(self.peers[source]["key"], self.node, source,
                                              normalized["x-telegent-nonce"], info["sha256"], info["size"])
        return info, response_headers

    def _quorum_action(self, action, payload):
        if action == "prepare": return self.quorum.prepare(**payload)
        if action == "accept": return self.quorum.accept(**payload)
        if action == "commit":
            return self.quorum.commit(payload["slot"], payload["previous_hash"],
                                      payload["value"], payload["receipts"])
        if action == "entries": return {"entries": self.quorum.entries(payload["after"], payload.get("limit", 100))}
        raise PeerError(400, "неизвестная операция кворума")

    def handle_quorum(self, action, body, headers):
        path = QUORUM_PREFIX + action
        source = verify_request(self.journal, {peer: value["key"] for peer, value in self.peers.items()},
                                "POST", path, body, headers, now=self.clock())
        try: payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError): raise PeerError(400, "запрос кворума повреждён")
        if not isinstance(payload, dict): raise PeerError(400, "запрос кворума должен быть объектом")
        response = json.dumps(self._quorum_action(action, payload), ensure_ascii=False,
                              sort_keys=True, separators=(",", ":")).encode("utf-8")
        nonce = {str(name).lower(): str(value) for name, value in headers.items()}["x-telegent-nonce"]
        return response, sign_response(self.peers[source]["key"], self.node, source, path, nonce, response)

    def quorum_exchange(self, peer, action, payload):
        if peer not in self.peers or action not in ("prepare", "accept", "commit", "entries"):
            raise PeerError(400, "некорректный quorum peer")
        path = QUORUM_PREFIX + action
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        nonce = os.urandom(16).hex()
        headers = sign_request(self.peers[peer]["key"], self.node, peer, "POST", path, body,
                               timestamp=int(self.clock()), nonce=nonce)
        response, response_headers = self._http_exchange_path(peer, path, body, headers)
        verify_response(self.peers[peer]["key"], peer, self.node, path, nonce, response, response_headers)
        try: value = json.loads(response.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError): raise PeerError(409, "ответ кворума повреждён")
        if not isinstance(value, dict): raise PeerError(409, "ответ кворума должен быть объектом")
        return value

    def consensus(self):
        return Consensus(self.quorum, self.quorum_exchange)

    def _http_exchange_path(self, peer, path, body, headers):
        request = urllib.request.Request(self.peers[peer]["url"] + path, data=body, headers=headers, method="POST")
        try:
            with _OPENER.open(request, timeout=20) as response:
                if int(response.headers.get("Content-Length") or 0) > MAX_BATCH_BYTES:
                    raise PeerError(413, "ответ соседнего узла слишком большой")
                result = response.read(MAX_BATCH_BYTES + 1)
                if len(result) > MAX_BATCH_BYTES: raise PeerError(413, "ответ соседнего узла слишком большой")
                return result, dict(response.headers.items())
        except PeerError: raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as error:
            raise PeerError(503, "соседний узел временно недоступен") from error

    def _http_exchange(self, peer, body, headers):
        return self._http_exchange_path(peer, SYNC_PATH, body, headers)

    def pull(self, peer, exchange=None, max_pages=100):
        if peer not in self.peers or type(max_pages) is not int or not 1 <= max_pages <= 1000:
            raise PeerError(400, "некорректный соседний узел")
        exchange = exchange or self._http_exchange
        inserted = 0
        for _ in range(max_pages):
            body = encode_sync_request(self.journal)
            nonce = os.urandom(16).hex()
            headers = sign_request(self.peers[peer]["key"], self.node, peer, "POST", SYNC_PATH,
                                   body, timestamp=int(self.clock()), nonce=nonce)
            response, response_headers = exchange(peer, body, headers)
            verify_response(self.peers[peer]["key"], peer, self.node, SYNC_PATH, nonce,
                            response, response_headers)
            result = apply_sync_response(self.journal, response)
            inserted += result["inserted"]
            if not result["more"]:
                return inserted
        raise PeerError(503, "синхронизация не завершилась за допустимое число пакетов")

    def pull_blob(self, peer, sha256, size, opener=None):
        if (peer not in self.peers or not valid_sha256(sha256) or type(size) is not int or
                not 0 <= size <= MAX_BLOB_BYTES):
            raise PeerError(400, "некорректный запрос файла у соседнего узла")
        if self.blobs.has(sha256, size):
            return self.blobs.path(sha256)
        body = json.dumps({"sha256": sha256}, separators=(",", ":")).encode("utf-8")
        nonce = os.urandom(16).hex()
        headers = sign_request(self.peers[peer]["key"], self.node, peer, "POST", BLOB_PATH,
                               body, timestamp=int(self.clock()), nonce=nonce)
        request = urllib.request.Request(self.peers[peer]["url"] + BLOB_PATH, data=body,
                                         headers=headers, method="POST")
        opener = opener or _OPENER
        try:
            with opener.open(request, timeout=60) as response:
                manifest = verify_blob_response(self.peers[peer]["key"], peer, self.node, nonce,
                                                dict(response.headers.items()))
                if manifest != {"sha256": sha256, "size": size}:
                    raise PeerError(409, "соседний узел ответил другим blob")
                try:
                    content_length = int(response.headers.get("Content-Length") or "")
                except ValueError:
                    raise PeerError(409, "у ответа blob нет корректной длины")
                if content_length != size:
                    raise PeerError(409, "длина ответа blob не совпадает")
                return self.blobs.receive(response, sha256, size)
        except PeerError:
            raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
            raise PeerError(503, "соседний blob временно недоступен") from error

    def _record(self, peer, error=None):
        with self.status_lock:
            state = self.status[peer]
            if error is None:
                state.update(last_success=self.clock(), last_error=None, failures=0)
            else:
                state.update(last_error=str(error), failures=state["failures"] + 1)

    def _run(self):
        while not self.stop_event.is_set():
            for peer in sorted(self.peers):
                if self.stop_event.is_set():
                    break
                try:
                    self.pull(peer)
                    self._record(peer)
                except PeerError as error:
                    self._record(peer, error)
            delay = self.poll_seconds * random.uniform(0.9, 1.1)
            self.stop_event.wait(delay)

    def report(self):
        with self.status_lock:
            peers = json.loads(json.dumps(self.status))
        return {"node": self.node, "slot": self.slot, "frontier": self.journal.frontier(), "peers": peers}
