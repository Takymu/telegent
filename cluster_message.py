"""Versioned codec for immutable message-create events.

Edits, deletion, secrets, permission changes and mutable status transitions are
not encoded here. They belong to the quorum log. Uploaded file bytes travel via
the separate blob transport; this codec only carries a verified manifest.
"""
import base64
import hashlib
import json
import re

from cluster_app import DeferredOperation, MAX_SAFE_INTEGER
from cluster_peer import PeerError


MESSAGE_COLUMNS = (
    "id", "ts", "author", "author_kind", "owner", "host", "recipients", "topic", "type",
    "priority", "subject", "body", "reply_to", "thread_id", "needs_approval", "status",
    "status_by", "status_ts", "status_ref", "status_note", "result", "idem_key", "approver",
    "revision", "mentions_json", "edited_ts", "deleted_ts", "conversation_id", "project_id",
)
ATTACHMENT_COLUMNS = ("id", "name", "size", "md5", "link", "upload_id")
UPLOAD_RE = re.compile(r"^u_[0-9a-f]{48}$")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _dict_row(row):
    return {key: row[key] for key in row.keys()}


def _safe_id(value, label):
    if type(value) is not int or not 0 < value <= MAX_SAFE_INTEGER:
        raise PeerError(409, "%s вышел за безопасный диапазон" % label)
    return value


def encode_message(connection, message_id, blob_manifest=None):
    row = connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    if row is None:
        raise PeerError(404, "сообщение для репликации не найдено")
    message = _dict_row(row)
    if connection.execute("SELECT 1 FROM secret_envelopes WHERE message_id=?", (message_id,)).fetchone():
        raise PeerError(425, "секретное сообщение требует кворумного протокола")
    if any(column not in message for column in MESSAGE_COLUMNS):
        raise PeerError(500, "схема сообщения не поддерживает кластерный кодек")
    message = {column: message[column] for column in MESSAGE_COLUMNS}
    _safe_id(message["id"], "id сообщения")
    attachments = []
    for row in connection.execute("SELECT * FROM attachments WHERE message_id=? ORDER BY id", (message_id,)):
        item = _dict_row(row)
        if any(column not in item for column in ATTACHMENT_COLUMNS):
            raise PeerError(500, "схема вложения не поддерживает кластерный кодек")
        encoded = {column: item[column] for column in ATTACHMENT_COLUMNS}
        _safe_id(encoded["id"], "id вложения")
        content = item.get("content")
        if content is not None:
            content = bytes(content)
            encoded["inline_b64"] = base64.b64encode(content).decode("ascii")
            encoded["sha256"] = hashlib.sha256(content).hexdigest()
        elif encoded["upload_id"]:
            if blob_manifest is None:
                raise PeerError(425, "для файлового вложения нужен проверенный манифест")
            manifest = blob_manifest(item)
            if (not isinstance(manifest, dict) or set(manifest) != {"sha256", "size"} or
                    not SHA256_RE.fullmatch(str(manifest["sha256"])) or manifest["size"] != encoded["size"]):
                raise PeerError(409, "манифест вложения не совпадает с метаданными")
            encoded.update(sha256=manifest["sha256"])
        elif not encoded["link"]:
            raise PeerError(409, "вложение не содержит данных, файла или ссылки")
        attachments.append(encoded)
    return {"version": 1, "message": message, "attachments": attachments}


def _validate_message(payload):
    if not isinstance(payload, dict) or set(payload) != {"version", "message", "attachments"}:
        raise PeerError(400, "некорректное событие создания сообщения")
    if payload["version"] != 1 or not isinstance(payload["message"], dict):
        raise PeerError(409, "неподдерживаемая версия события сообщения")
    message = payload["message"]
    if set(message) != set(MESSAGE_COLUMNS) or not isinstance(payload["attachments"], list):
        raise PeerError(400, "некорректные поля события сообщения")
    _safe_id(message["id"], "id сообщения")
    if message["reply_to"] is not None:
        _safe_id(message["reply_to"], "id родительского сообщения")
    _safe_id(message["thread_id"], "id ветки")
    if message["author_kind"] not in ("human", "agent") or type(message["project_id"]) is not int:
        raise PeerError(400, "некорректная область сообщения")
    if type(message["revision"]) is not int or message["revision"] != 0:
        raise PeerError(409, "создаваемое сообщение должно иметь начальную версию")
    if message["edited_ts"] is not None or message["deleted_ts"] is not None:
        raise PeerError(409, "правка или удаление не входят в событие создания")
    try:
        mentions = json.loads(message["mentions_json"])
    except (TypeError, json.JSONDecodeError):
        raise PeerError(400, "упоминания сообщения повреждены")
    if not isinstance(mentions, list):
        raise PeerError(400, "упоминания сообщения повреждены")
    if len(payload["attachments"]) > 30:
        raise PeerError(400, "слишком много вложений")
    return message


def _validate_attachment(item):
    allowed = set(ATTACHMENT_COLUMNS) | {"inline_b64", "sha256"}
    if not isinstance(item, dict) or set(item) - allowed or set(ATTACHMENT_COLUMNS) - set(item):
        raise PeerError(400, "некорректное вложение события")
    _safe_id(item["id"], "id вложения")
    if not isinstance(item["name"], str) or not item["name"] or len(item["name"]) > 200:
        raise PeerError(400, "некорректное имя вложения")
    if item["size"] is not None and (type(item["size"]) is not int or item["size"] < 0):
        raise PeerError(400, "некорректный размер вложения")
    if item["md5"] is not None and not MD5_RE.fullmatch(str(item["md5"])):
        raise PeerError(400, "некорректный md5 вложения")
    modes = sum((item.get("inline_b64") is not None, bool(item.get("upload_id")), bool(item.get("link"))))
    if modes != 1:
        raise PeerError(409, "вложение должно иметь ровно один источник")
    if (item.get("inline_b64") is not None or item.get("upload_id")) and type(item["size"]) is not int:
        raise PeerError(400, "для сохранённого вложения нужен размер")
    if item.get("upload_id") and not UPLOAD_RE.fullmatch(str(item["upload_id"])):
        raise PeerError(400, "некорректный id файлового вложения")
    if (item.get("inline_b64") is not None or item.get("upload_id")) and not SHA256_RE.fullmatch(str(item.get("sha256"))):
        raise PeerError(400, "у вложения нет sha256")


def apply_message(connection, payload, blob_ready=None):
    """Materialize one verified create event in the caller's transaction."""
    message = _validate_message(payload)
    for item in payload["attachments"]:
        _validate_attachment(item)
    existing = connection.execute("SELECT * FROM messages WHERE id=?", (message["id"],)).fetchone()
    if existing is not None:
        current = _dict_row(existing)
        if any(current.get(column) != message[column] for column in MESSAGE_COLUMNS):
            raise PeerError(409, "id сообщения уже занят другим содержимым")
    if existing is None and not connection.execute("SELECT 1 FROM projects WHERE id=?", (message["project_id"],)).fetchone():
        raise DeferredOperation()
    if existing is None and not connection.execute("SELECT 1 FROM users WHERE name=?", (message["author"],)).fetchone():
        raise DeferredOperation()
    if existing is None and not connection.execute("SELECT 1 FROM members WHERE project_id=? AND user=?",
                              (message["project_id"], message["author"])).fetchone():
        raise DeferredOperation()
    if existing is None and message["conversation_id"] is not None and not connection.execute(
            "SELECT 1 FROM conversations WHERE id=? AND project_id=?",
            (message["conversation_id"], message["project_id"])).fetchone():
        raise DeferredOperation()
    if existing is None and message["reply_to"] is not None and not connection.execute(
            "SELECT 1 FROM messages WHERE id=? AND project_id=?", (message["reply_to"], message["project_id"])).fetchone():
        raise DeferredOperation()
    for item in payload["attachments"]:
        if item.get("upload_id") and (blob_ready is None or not blob_ready(item)):
            raise DeferredOperation()

    if existing is None:
        columns = ",".join(MESSAGE_COLUMNS)
        placeholders = ",".join("?" for _ in MESSAGE_COLUMNS)
        connection.execute("INSERT INTO messages(" + columns + ") VALUES(" + placeholders + ")",
                           tuple(message[column] for column in MESSAGE_COLUMNS))
    for item in payload["attachments"]:
        content = None
        if item.get("inline_b64") is not None:
            try:
                content = base64.b64decode(item["inline_b64"], validate=True)
            except (ValueError, TypeError):
                raise PeerError(400, "inline-вложение повреждено")
            if (len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"] or
                    item["md5"] is not None and hashlib.md5(content).hexdigest() != item["md5"]):
                raise PeerError(409, "контрольная сумма inline-вложения не совпадает")
        stored = connection.execute("SELECT * FROM attachments WHERE id=?", (item["id"],)).fetchone()
        expected = (message["id"], item["name"], item["size"], item["md5"], item["link"], item["upload_id"])
        if stored is not None:
            actual = tuple(stored[key] for key in ("message_id", "name", "size", "md5", "link", "upload_id"))
            if actual != expected or ((content is None) != (stored["content"] is None)) or (
                    content is not None and bytes(stored["content"]) != content):
                raise PeerError(409, "id вложения уже занят другим содержимым")
        else:
            connection.execute("INSERT INTO attachments(id,message_id,name,size,md5,link,content,upload_id) "
                               "VALUES(?,?,?,?,?,?,?,?)",
                               (item["id"], message["id"], item["name"], item["size"], item["md5"],
                                item["link"], content, item["upload_id"]))
        if item.get("upload_id"):
            connection.execute("""INSERT OR IGNORE INTO file_uploads
                (id,project_id,user,name,size,received,md5,expected_md5,idem_key,created,updated,attachment_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item["upload_id"], message["project_id"], message["author"], item["name"], item["size"],
                 item["size"], item["md5"], item["md5"], None, message["ts"], message["ts"], item["id"]))
            upload = connection.execute("SELECT * FROM file_uploads WHERE id=?", (item["upload_id"],)).fetchone()
            expected_upload = (message["project_id"], message["author"], item["name"], item["size"],
                               item["size"], item["md5"], item["id"])
            actual_upload = tuple(upload[key] for key in
                                  ("project_id", "user", "name", "size", "received", "md5", "attachment_id"))
            if actual_upload != expected_upload:
                raise PeerError(409, "id файлового вложения уже занят другим содержимым")
