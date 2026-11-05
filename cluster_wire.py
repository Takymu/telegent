"""Authenticated peer-to-peer journal exchange for telegent server nodes.

The module is transport-framework agnostic: server.py can pass HTTP headers/body
to ``verify_request`` and peers can use ``sign_request`` for an outbound request.
Pairwise keys authenticate the channel; journal hashes authenticate continuity.
"""
import hashlib
import hmac
import json
import math
import secrets
import time

from cluster_peer import MAX_BATCH_BYTES, PeerError


MAX_CLOCK_SKEW = 300
SYNC_PATH = "/api/cluster/sync"


def _body_bytes(value):
    try:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PeerError(400, "сетевой пакет нельзя сериализовать")
    if len(body) > MAX_BATCH_BYTES:
        raise PeerError(413, "сетевой пакет слишком большой")
    return body


def _key(value):
    if not isinstance(value, (bytes, bytearray)) or len(value) < 32:
        raise PeerError(500, "ключ связи между узлами настроен неверно")
    return bytes(value)


def _material(source, target, method, path, timestamp, nonce, body):
    if method not in ("GET", "POST") or not path.startswith("/api/cluster/"):
        raise PeerError(400, "некорректный сетевой маршрут кластера")
    parts = [source, target, method, path, str(timestamp), nonce, hashlib.sha256(body).hexdigest()]
    return "\n".join(parts).encode("utf-8")


def sign_request(key, source, target, method, path, body, timestamp=None, nonce=None):
    """Return headers for one request. ``body`` is the exact transmitted bytes."""
    if not isinstance(body, bytes):
        raise PeerError(400, "тело сетевого запроса должно быть байтами")
    if timestamp is None:
        timestamp = int(time.time())
    if type(timestamp) is not int:
        raise PeerError(400, "некорректное время сетевого запроса")
    if nonce is None:
        nonce = secrets.token_hex(16)
    material = _material(source, target, method, path, timestamp, nonce, body)
    signature = hmac.new(_key(key), material, hashlib.sha256).hexdigest()
    return {
        "X-Telegent-Node": source,
        "X-Telegent-Target": target,
        "X-Telegent-Time": str(timestamp),
        "X-Telegent-Nonce": nonce,
        "X-Telegent-Signature": signature,
        "Content-Type": "application/json; charset=utf-8",
    }


def _response_material(source, target, path, request_nonce, body):
    parts = [source, target, path, request_nonce, hashlib.sha256(body).hexdigest()]
    return "\n".join(parts).encode("utf-8")


def sign_response(key, source, target, path, request_nonce, body):
    """Bind a response to the exact authenticated request nonce and body."""
    if not isinstance(body, bytes):
        raise PeerError(400, "тело сетевого ответа должно быть байтами")
    signature = hmac.new(_key(key), _response_material(source, target, path, request_nonce, body),
                         hashlib.sha256).hexdigest()
    return {
        "X-Telegent-Node": source,
        "X-Telegent-Target": target,
        "X-Telegent-Request-Nonce": request_nonce,
        "X-Telegent-Signature": signature,
        "Content-Type": "application/json; charset=utf-8",
    }


def verify_response(key, source, target, path, request_nonce, body, headers):
    if not isinstance(body, bytes) or not isinstance(headers, dict):
        raise PeerError(400, "некорректный сетевой ответ")
    normalized = {str(name).lower(): str(value) for name, value in headers.items()}
    if (normalized.get("x-telegent-node") != source or
            normalized.get("x-telegent-target") != target or
            normalized.get("x-telegent-request-nonce") != request_nonce):
        raise PeerError(401, "сетевой ответ кластера не подтверждён")
    expected = hmac.new(_key(key), _response_material(source, target, path, request_nonce, body),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(normalized.get("x-telegent-signature", ""), expected):
        raise PeerError(401, "сетевой ответ кластера не подтверждён")


def verify_request(journal, peer_keys, method, path, body, headers, now=None):
    """Authenticate and durably consume a request nonce; return the source node."""
    if not isinstance(body, bytes) or not isinstance(headers, dict):
        raise PeerError(400, "некорректный сетевой запрос")
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    source = normalized.get("x-telegent-node", "")
    target = normalized.get("x-telegent-target", "")
    nonce = normalized.get("x-telegent-nonce", "")
    signature = normalized.get("x-telegent-signature", "")
    try:
        timestamp = int(normalized.get("x-telegent-time", ""))
    except ValueError:
        raise PeerError(401, "сетевой запрос кластера не подтверждён")
    if target != journal.node or source == target or source not in journal.members:
        raise PeerError(401, "сетевой запрос кластера не подтверждён")
    if now is None:
        now = time.time()
    if type(now) not in (int, float) or not math.isfinite(now):
        raise PeerError(503, "узел не может проверить время сетевого запроса")
    now = int(now)
    if abs(now - timestamp) > MAX_CLOCK_SKEW:
        raise PeerError(401, "время сетевого запроса кластера вышло за допустимое окно")
    try:
        key = peer_keys[source]
    except (KeyError, TypeError):
        raise PeerError(401, "сетевой запрос кластера не подтверждён")
    expected = hmac.new(_key(key), _material(source, target, method, path, timestamp, nonce, body),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise PeerError(401, "сетевой запрос кластера не подтверждён")
    journal.accept_nonce(source, nonce, timestamp, now - MAX_CLOCK_SKEW)
    return source


def encode_sync_request(journal):
    return _body_bytes({"frontier": journal.frontier()})


def decode_sync_request(body, members):
    if not isinstance(body, bytes) or len(body) > MAX_BATCH_BYTES:
        raise PeerError(413, "сетевой пакет слишком большой")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise PeerError(400, "сетевой пакет повреждён")
    if not isinstance(value, dict) or set(value) != {"frontier"} or not isinstance(value["frontier"], dict):
        raise PeerError(400, "некорректная граница журналов")
    result = {}
    for origin, position in value["frontier"].items():
        if origin not in members or not isinstance(position, dict) or set(position) != {"sequence", "hash"}:
            raise PeerError(400, "некорректная граница журналов")
        sequence = position["sequence"]
        digest = position["hash"]
        if (type(sequence) is not int or sequence < 0 or not isinstance(digest, str) or len(digest) != 64 or
                any(character not in "0123456789abcdef" for character in digest)):
            raise PeerError(400, "некорректная граница журналов")
        result[origin] = {"sequence": sequence, "hash": digest}
    if set(result) != set(members):
        raise PeerError(409, "состав узлов в сетевом запросе не совпадает")
    return result


def build_sync_response(journal, remote_frontier, max_events=200, max_bytes=MAX_BATCH_BYTES):
    if type(max_events) is not int or not 1 <= max_events <= 1000:
        raise PeerError(400, "некорректный размер пакета синхронизации")
    if type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_BATCH_BYTES:
        raise PeerError(400, "некорректный предел пакета синхронизации")
    local = journal.frontier()
    events = []
    used = 0
    next_positions = {origin: remote_frontier[origin]["sequence"] for origin in journal.members}
    for origin in journal.members:
        remote = remote_frontier[origin]
        if remote["sequence"] > local[origin]["sequence"]:
            continue
        if journal.hash_at(origin, remote["sequence"]) != remote["hash"]:
            raise PeerError(409, "цепочки узлов расходятся")
        for event in journal.export(origin, remote["sequence"], min(1000, max_events - len(events))):
            raw = _body_bytes(event)
            if events and used + len(raw) > max_bytes:
                break
            if not events and len(raw) > max_bytes:
                raise PeerError(413, "одно событие не помещается в пакет синхронизации")
            events.append(event)
            used += len(raw)
            next_positions[origin] = event["sequence"]
            if len(events) >= max_events:
                break
        if len(events) >= max_events or used >= max_bytes:
            break
    more = any(next_positions[origin] < local[origin]["sequence"] for origin in journal.members)
    return _body_bytes({"events": events, "frontier": local, "more": more})


def apply_sync_response(journal, body):
    if not isinstance(body, bytes) or len(body) > MAX_BATCH_BYTES:
        raise PeerError(413, "сетевой пакет слишком большой")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise PeerError(400, "сетевой пакет повреждён")
    if not isinstance(value, dict) or set(value) != {"events", "frontier", "more"}:
        raise PeerError(400, "некорректный ответ синхронизации")
    if type(value["more"]) is not bool or not isinstance(value["events"], list):
        raise PeerError(400, "некорректный ответ синхронизации")
    remote = decode_sync_request(_body_bytes({"frontier": value["frontier"]}), journal.members)
    before = journal.frontier()
    for origin, position in remote.items():
        if position["sequence"] <= before[origin]["sequence"]:
            if journal.hash_at(origin, position["sequence"]) != position["hash"]:
                raise PeerError(409, "цепочки узлов расходятся")
    inserted = journal.ingest(value["events"])
    local = journal.frontier()
    for origin, position in local.items():
        remote_position = remote[origin]
        if position["sequence"] == remote_position["sequence"] and position["hash"] != remote_position["hash"]:
            raise PeerError(409, "цепочки узлов расходятся")
    return {"inserted": inserted, "more": value["more"], "remote_frontier": remote}
