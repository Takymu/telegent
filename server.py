#!/usr/bin/env python3
"""telegent server: a small message bus for a team's agents and humans.

Stdlib only (Python 3.8+): HTTP, SQLite metadata and a sibling directory for files.

    python server.py adduser alice-agent --kind agent --owner alice  # prints the token once
    python server.py serve --port 8765                              # http://127.0.0.1:8765/

See README.md for hosting (tunnel) and AGENTS.md for the agent protocol.
"""
import argparse
import base64
import codecs
import ctypes
import ctypes.util
import hashlib
import io
import json
import os
import queue
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie, CookieError

VERSION = "2.0"
HERE = os.path.dirname(os.path.abspath(__file__))

USER_TYPES = ("info", "request", "question", "result", "alert", "decision")
SYSTEM_TYPES = ("status", "approval")
TRACKED_TYPES = ("request", "alert", "decision")      # these carry open/taken/done/rejected
STATES = ("open", "taken", "done", "rejected")
PRIORITIES = ("normal", "urgent")
REACTION_EMOJI = ("❤️", "🔥", "👍", "👎", "🤡", "😁", "😭")
RESULT_FIELDS = ("metric", "dataset", "value", "err", "baseline", "commit", "weights", "note")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")

MAX_REQUEST = 40 * 1024 * 1024
MAX_BODY_CHARS = 200_000
MAX_FILE_BYTES = 200 * 1024 * 1024
UPLOAD_CHUNK = 4 * 1024 * 1024
MIN_FREE_BYTES = 300 * 1024 * 1024
DOWNLOADS = {}

# ---------------------------------------------------------------- secret filter
# Same list lives in tg.py (client checks first, server enforces).
SECRET_PATTERNS = [
    ("private key", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    ("AWS access key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    ("API key (sk-...)", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    ("HuggingFace token", r"\bhf_[A-Za-z0-9]{30,}"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("Telegram bot token", r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    ("JWT", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("telegent token", r"\btg[tsi]_[0-9a-f]{32,}"),
    ("password in URL", r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]{3,}@"),
    ("sshpass -p", r"\bsshpass\s+-p\s*\S+"),
    ("assigned secret", r"(?i)\b(?:password|passwd|pwd|passphrase|пароль|secret|token|api[_-]?key|"
                        r"access[_-]?key|private[_-]?key|client[_-]?secret)\b[\"']?\s*[:=]\s*[\"']?"
                        r"(?![\s$<{%*]|\.\.\.|os\.environ|os\.getenv|getenv|env\b|ENV\b|none\b|None\b|null\b)"
                        r"[^\s\"',;]{6,}"),
]
_SECRET_RES = [(n, re.compile(p)) for n, p in SECRET_PATTERNS]


def find_secret(text, literals=()):
    """Return (kind, line_no) of the first secret-looking thing in text, or None. Never returns the secret."""
    if not text:
        return None
    for kind, rx in _SECRET_RES:
        m = rx.search(text)
        if m:
            return kind, text.count("\n", 0, m.start()) + 1
    for lit in literals:
        i = text.find(lit)
        if i >= 0:
            return "строка из списка секретов сервера", text.count("\n", 0, i) + 1
    return None


# ---------------------------------------------------------------- config / time
class Cfg:
    db = os.path.join(HERE, "telegent.db")
    tz = timezone(timedelta(hours=7))
    deny_literals = []
    max_inline = 2 * 1024 * 1024
    telegram = None   # {"bot_token": ..., "chat_id": ...}
    launcher_url = ""  # permanent entry page that remembers the login across tunnel addresses
    # the project that `adduser` without --project uses, and where a pre-projects database moves to
    default_project = {"slug": "main", "title": "main"}


def load_config(path):
    if not path or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        c = json.load(f)
    if "tz_offset_hours" in c:
        Cfg.tz = timezone(timedelta(hours=float(c["tz_offset_hours"])))
    if c.get("db"):
        Cfg.db = c["db"] if os.path.isabs(c["db"]) else os.path.join(os.path.dirname(path), c["db"])
    if c.get("max_inline_bytes"):
        Cfg.max_inline = int(c["max_inline_bytes"])
    if c.get("deny_file"):
        p = c["deny_file"] if os.path.isabs(c["deny_file"]) else os.path.join(os.path.dirname(path), c["deny_file"])
        with open(p, encoding="utf-8") as f:
            Cfg.deny_literals = [ln.strip() for ln in f if len(ln.strip()) >= 6 and not ln.startswith("#")]
    tg = c.get("telegram")
    if tg and tg.get("bot_token") and tg.get("chat_id"):
        Cfg.telegram = tg
    Cfg.launcher_url = c.get("launcher_url") or ""
    if isinstance(c.get("default_project"), dict) and c["default_project"].get("slug"):
        Cfg.default_project = {"slug": c["default_project"]["slug"],
                               "title": c["default_project"].get("title") or c["default_project"]["slug"]}


def iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, Cfg.tz).isoformat(timespec="seconds")


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  name TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
  token_hash TEXT UNIQUE NOT NULL, created_ts REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, author TEXT NOT NULL, author_kind TEXT NOT NULL, owner TEXT NOT NULL, host TEXT,
  recipients TEXT NOT NULL DEFAULT '', topic TEXT NOT NULL DEFAULT '', type TEXT NOT NULL,
  priority TEXT NOT NULL DEFAULT 'normal', subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
  reply_to INTEGER, thread_id INTEGER, needs_approval INTEGER NOT NULL DEFAULT 0,
  status TEXT, status_by TEXT, status_ts REAL, status_ref TEXT, status_note TEXT,
  result TEXT, idem_key TEXT, approver TEXT,
  UNIQUE(author, idem_key));
CREATE INDEX IF NOT EXISTS ix_msg_thread ON messages(thread_id);
CREATE TABLE IF NOT EXISTS approvals(
  message_id INTEGER NOT NULL, user TEXT NOT NULL, decision TEXT NOT NULL, note TEXT, ts REAL NOT NULL,
  PRIMARY KEY(message_id, user));
CREATE TABLE IF NOT EXISTS approval_history(
  message_id INTEGER NOT NULL, revision INTEGER NOT NULL, user TEXT NOT NULL,
  decision TEXT NOT NULL, note TEXT, ts REAL NOT NULL, body_hash TEXT NOT NULL,
  PRIMARY KEY(message_id, revision, user));
CREATE TABLE IF NOT EXISTS pending_file_deletions(upload_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS attachments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, name TEXT NOT NULL,
  size INTEGER, md5 TEXT, link TEXT, content BLOB);
CREATE INDEX IF NOT EXISTS ix_att_msg ON attachments(message_id);
CREATE TABLE IF NOT EXISTS conversations(
  id TEXT PRIMARY KEY, project_id INTEGER NOT NULL, human TEXT NOT NULL, agent TEXT NOT NULL,
  created REAL NOT NULL, human_read_upto INTEGER NOT NULL DEFAULT 0, agent_read_upto INTEGER NOT NULL DEFAULT 0,
  UNIQUE(project_id,human,agent));
CREATE TABLE IF NOT EXISTS secret_envelopes(
  message_id INTEGER PRIMARY KEY, recipient TEXT NOT NULL, key_id TEXT NOT NULL,
  envelope TEXT, expires REAL NOT NULL, claimed_by TEXT, claimed_ts REAL, consumed REAL, revoked REAL);
CREATE TABLE IF NOT EXISTS file_uploads(
  id TEXT PRIMARY KEY, project_id INTEGER NOT NULL, user TEXT NOT NULL, name TEXT NOT NULL,
  size INTEGER NOT NULL, received INTEGER NOT NULL DEFAULT 0, md5 TEXT, expected_md5 TEXT,
  idem_key TEXT, created REAL NOT NULL, updated REAL NOT NULL, attachment_id INTEGER,
  UNIQUE(project_id, user, idem_key));
CREATE TABLE IF NOT EXISTS receipts(
  user TEXT NOT NULL, message_id INTEGER NOT NULL, read_ts REAL, ack_ts REAL,
  PRIMARY KEY(user, message_id));
CREATE TABLE IF NOT EXISTS waiting_dismissals(
  user TEXT NOT NULL, message_id INTEGER NOT NULL, revision INTEGER NOT NULL,
  PRIMARY KEY(user, message_id));
CREATE TABLE IF NOT EXISTS reactions(
  message_id INTEGER NOT NULL, user TEXT NOT NULL, emoji TEXT NOT NULL, ts REAL NOT NULL,
  PRIMARY KEY(message_id, user, emoji));
CREATE TABLE IF NOT EXISTS board(
  project_id INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL, now TEXT, next TEXT, gpu_until TEXT, note TEXT, ts REAL,
  PRIMARY KEY(project_id, name));
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
  created_by TEXT NOT NULL, created_ts REAL NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS members(
  project_id INTEGER NOT NULL, user TEXT NOT NULL, role TEXT NOT NULL, added_by TEXT, added_ts REAL,
  PRIMARY KEY(project_id, user));
CREATE INDEX IF NOT EXISTS ix_members_user ON members(user);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user TEXT NOT NULL, created_ts REAL NOT NULL, last_ts REAL, label TEXT);
CREATE TABLE IF NOT EXISTS invites(
  code_hash TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, owner TEXT NOT NULL, purpose TEXT NOT NULL,
  created_by TEXT NOT NULL, created_ts REAL NOT NULL, expires_ts REAL NOT NULL, used_ts REAL);
CREATE TABLE IF NOT EXISTS claims(
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, display_key TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL, owner_kind TEXT NOT NULL, host TEXT, note TEXT, until TEXT, created REAL NOT NULL,
  released REAL, release_note TEXT, release_msg INTEGER);
CREATE INDEX IF NOT EXISTS ix_claims_key ON claims(key);
"""

DB_LOCK = threading.RLock()
UPLOAD_LOCK = DB_LOCK
NEW_MSG = threading.Condition()
LAST_ID = [0]
REACTION_EPOCH = secrets.token_hex(8)
REACTION_REVISIONS = {}
RECEIPT_REVISIONS = {}
CONTENT_REVISIONS = {}
_db = None
TG_QUEUE = queue.Queue()


def db():
    global _db
    if _db is None:
        _db = sqlite3.connect(Cfg.db, check_same_thread=False, isolation_level=None)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA busy_timeout=5000")
        _db.executescript(SCHEMA)
        for table, col, decl in (("receipts", "delivered_ts", "REAL"), ("users", "last_seen_ts", "REAL"),
                                 ("messages", "revision", "INTEGER NOT NULL DEFAULT 0"),
                                 ("messages", "edited_ts", "REAL"), ("messages", "deleted_ts", "REAL"),
                                 ("users", "last_wait_ts", "REAL"), ("users", "pw_hash", "TEXT"),
                                 ("users", "last_check_ts", "REAL"),
                                 ("users", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
                                 ("members", "read_upto", "INTEGER NOT NULL DEFAULT 0"),
                                 ("members", "sidebar_position", "INTEGER"),
                                 ("messages", "conversation_id", "TEXT"),
                                 ("attachments", "upload_id", "TEXT"),
                                 ("projects", "admin_owner", "TEXT"), ("projects", "service_agent", "TEXT"),
                                 ("messages", "project_id", "INTEGER"), ("messages", "approver", "TEXT"),
                                 ("claims", "project_id", "INTEGER"),
                                 ("claims", "expires", "REAL"),
                                 ("invites", "project_id", "INTEGER"), ("invites", "role", "TEXT")):
            if col not in {r[1] for r in _db.execute(f"PRAGMA table_info({table})")}:
                _db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        if "creator_unknown" not in {r[1] for r in _db.execute("PRAGMA table_info(projects)")}:
            _db.execute("BEGIN IMMEDIATE")
            try:
                _db.execute("ALTER TABLE projects ADD COLUMN creator_unknown INTEGER NOT NULL DEFAULT 0")
                # Old migration/console markers are not authenticated account identities.
                _db.execute("UPDATE projects SET creator_unknown = 1 WHERE created_by IN ('migration', 'cli')")
                _db.execute("COMMIT")
            except Exception:
                _db.execute("ROLLBACK")
                raise
        _db.executescript("CREATE INDEX IF NOT EXISTS ix_msg_project ON messages(project_id, id);")
        _db.execute("CREATE INDEX IF NOT EXISTS ix_msg_conversation ON messages(conversation_id,id)")
        if "project_id" not in {r[1] for r in _db.execute("PRAGMA table_info(board)")}:
            # the board used to be one per server: rebuild it keyed by (project, name)
            _db.executescript("""
                ALTER TABLE board RENAME TO board_old;
                CREATE TABLE board(project_id INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL, now TEXT, next TEXT,
                  gpu_until TEXT, note TEXT, ts REAL, PRIMARY KEY(project_id, name));
                INSERT INTO board(project_id, name, now, next, gpu_until, note, ts)
                  SELECT 0, name, now, next, gpu_until, note, ts FROM board_old;
                DROP TABLE board_old;""")
        if "back" not in {r[1] for r in _db.execute("PRAGMA table_info(board)")}:
            _db.execute("ALTER TABLE board ADD COLUMN back TEXT")
        migrate_identities(_db)
        _db.create_function("pylower", 1, lambda s: s.lower() if isinstance(s, str) else s)
        ensure_default_project(_db)
        # messages used to have a separate subject; now it is the first line of the text (idempotent)
        _db.execute("UPDATE messages SET body = subject || CASE WHEN body = '' THEN '' ELSE char(10) || body END "
                    "WHERE deleted_ts IS NULL AND type NOT IN ('status', 'approval') AND subject != '' "
                    "AND substr(ltrim(body, ' ' || char(9) || char(10) || char(13)), 1, length(subject)) != subject")
        migrate_reactions(_db)
        LAST_ID[0] = _db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
    return _db


def migrate_reactions(con):
    """Merge legacy agreement into the same person's like without duplicating it."""
    if not con.execute("SELECT 1 FROM reactions WHERE emoji='+1' LIMIT 1").fetchone():
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("INSERT OR IGNORE INTO reactions(message_id,user,emoji,ts) "
                    "SELECT message_id,user,'👍',ts FROM reactions WHERE emoji='+1'")
        con.execute("DELETE FROM reactions WHERE emoji='+1'")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def migrate_identities(con):
    """Keep every legacy account key/reference; names become separate, scoped labels."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if "display_name" not in {r[1] for r in con.execute("PRAGMA table_info(users)")}:
            con.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
            con.execute("ALTER TABLE users ADD COLUMN agent_project_id INTEGER")
            con.execute("UPDATE users SET display_name = name")
        if "account_id" not in {r[1] for r in con.execute("PRAGMA table_info(invites)")}:
            con.execute("ALTER TABLE invites ADD COLUMN account_id TEXT")
            con.execute("UPDATE invites SET account_id = name WHERE purpose IN ('add','reset')")
        con.execute("UPDATE users SET agent_project_id = (SELECT project_id FROM members WHERE user=users.name) "
                    "WHERE kind='agent'")
        if con.execute("SELECT 1 FROM members m JOIN users u ON u.name=m.user WHERE u.kind='agent' "
                       "GROUP BY m.user HAVING COUNT(*)>1").fetchone():
            raise RuntimeError("миграция: агент состоит в нескольких проектах; нужно исправить членства")
        if con.execute("SELECT 1 FROM members m JOIN users u ON u.name=m.user GROUP BY m.project_id, "
                       "lower(COALESCE(u.display_name,u.name)) HAVING COUNT(*)>1").fetchone():
            raise RuntimeError("миграция: совпадающие имена внутри проекта; нужно решение владельца")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_human_login ON users "
                    "(COALESCE(display_name,name) COLLATE NOCASE) WHERE kind='human'")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_project_name ON users "
                    "(agent_project_id, COALESCE(display_name,name) COLLATE NOCASE) WHERE kind='agent'")
        # Triggers keep the denormalized index and cross-kind member names consistent.
        statements = [
            "CREATE TRIGGER IF NOT EXISTS account_identity_update BEFORE UPDATE OF name ON users "
            "WHEN NEW.name!=OLD.name BEGIN SELECT RAISE(ABORT,'account identity is immutable'); END",
            "CREATE TRIGGER IF NOT EXISTS member_identity_insert BEFORE INSERT ON members BEGIN "
            "SELECT RAISE(ABORT,'project name already in use') WHERE EXISTS "
            "(SELECT 1 FROM members m JOIN users other ON other.name=m.user JOIN users incoming ON incoming.name=NEW.user "
            "WHERE m.project_id=NEW.project_id AND m.user!=NEW.user AND "
            "lower(COALESCE(other.display_name,other.name))=lower(COALESCE(incoming.display_name,incoming.name))); "
            "SELECT RAISE(ABORT,'agent belongs to another project') WHERE EXISTS "
            "(SELECT 1 FROM users u JOIN members m ON m.user=u.name WHERE u.name=NEW.user "
            "AND u.kind='agent' AND m.project_id!=NEW.project_id); END",
            "CREATE TRIGGER IF NOT EXISTS member_identity_scope_insert AFTER INSERT ON members BEGIN "
            "UPDATE users SET agent_project_id=NEW.project_id WHERE name=NEW.user AND kind='agent'; END",
            "CREATE TRIGGER IF NOT EXISTS member_identity_scope_delete AFTER DELETE ON members BEGIN "
            "UPDATE users SET agent_project_id=(SELECT project_id FROM members WHERE user=OLD.user) "
            "WHERE name=OLD.user AND kind='agent'; END",
            "CREATE TRIGGER IF NOT EXISTS member_identity_update BEFORE UPDATE OF user,project_id ON members "
            "WHEN NEW.user!=OLD.user OR NEW.project_id!=OLD.project_id BEGIN "
            "SELECT RAISE(ABORT,'membership identity is immutable; remove and add it'); END",
            "CREATE TRIGGER IF NOT EXISTS account_name_update BEFORE UPDATE OF display_name ON users BEGIN "
            "SELECT RAISE(ABORT,'project name already in use') WHERE EXISTS "
            "(SELECT 1 FROM members mine JOIN members m ON m.project_id=mine.project_id JOIN users other ON other.name=m.user "
            "WHERE mine.user=OLD.name AND m.user!=OLD.name AND "
            "lower(COALESCE(other.display_name,other.name))=lower(COALESCE(NEW.display_name,NEW.name))); END",
        ]
        for statement in statements:
            con.execute(statement)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def account_name(key):
    if not key:
        return key
    row = db().execute("SELECT COALESCE(display_name,name) FROM users WHERE name=?", (key,)).fetchone()
    return row[0] if row else key


def account_dict(row):
    return {"id": row["name"], "name": row["display_name"] or row["name"], "kind": row["kind"],
            "owner": account_name(row["owner"]), "owner_id": row["owner"]}


def find_accounts(label=None, pid=None, kind=None, account_id=None, owner=None):
    if (label is not None and not isinstance(label, str)) or (account_id is not None and not isinstance(account_id, str)):
        raise ApiError(400, _ui_text('имя и account_id должны быть строками'))
    sql, args = "SELECT u.* FROM users u WHERE 1=1", []
    if account_id is not None:
        sql += " AND u.name=?"; args.append(account_id)
    else:
        sql += " AND lower(COALESCE(u.display_name,u.name))=lower(?)"; args.append(label or "")
    if pid is not None:
        sql += " AND EXISTS (SELECT 1 FROM members m WHERE m.user=u.name AND m.project_id=?)"; args.append(pid)
    if kind:
        sql += " AND u.kind=?"; args.append(kind)
    if owner:
        sql += " AND u.owner=?"; args.append(owner)
    return db().execute(sql, args).fetchall()


def resolve_account(label=None, pid=None, kind=None, account_id=None, owner=None):
    rows = find_accounts(label, pid, kind, account_id, owner)
    if not rows:
        raise ApiError(404, _ui_text('аккаунта нет в выбранном проекте или доступном списке'))
    if len(rows) != 1:
        raise ApiError(409, _ui_text('имя неоднозначно: выбери проект или укажи account_id'))
    return rows[0]


def new_account(con, label, kind, owner, token, password=None):
    # A legacy-looking key is harmless: it is never reused or resolved as a display name.
    key = label if not con.execute("SELECT 1 FROM users WHERE name=?", (label,)).fetchone() else "acct:"+secrets.token_hex(16)
    if kind == "human":
        owner = key
    con.execute("INSERT INTO users(name,display_name,kind,owner,token_hash,created_ts,pw_hash) VALUES (?,?,?,?,?,?,?)",
                (key, label, kind, owner, token_hash(token), time.time(), password))
    return key


def check_member_name(con, pid, label, excluding=None):
    if con.execute("SELECT 1 FROM members m JOIN users u ON u.name=m.user WHERE m.project_id=? "
                   "AND lower(COALESCE(u.display_name,u.name))=lower(?) AND u.name!=?",
                   (pid, label, excluding or "")).fetchone():
        raise ApiError(409, _ui_text('это имя уже занято в проекте'))


def managed_account(h, user, q, data):
    """Resolve an action target in its project, or an explicitly owned detached account."""
    label, key = str(data.get("name") or account_name(user["name"])).strip(), data.get("account_id")
    explicit = bool(q.get("project") or h.headers.get("X-Telegent-Project"))
    try:
        project = project_for(h, user, q)
    except ApiError:
        if explicit:
            raise
        project = None
    if project:
        rows = find_accounts(label, project["id"], account_id=key)
        if rows:
            return rows[0]
    # Detached agents are listed to their person by stable id, never borrowed by name from another project.
    rows = find_accounts(label, account_id=key, owner=user["name"], kind="agent")
    rows = [r for r in rows if not db().execute("SELECT 1 FROM members WHERE user=?", (r["name"],)).fetchone()]
    if not explicit:
        if key == user["name"] or (not key and label.lower() == account_name(user["name"]).lower()):
            return user
        if user["is_admin"]:
            rows = find_accounts(label, account_id=key)
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        raise ApiError(409, _ui_text('имя неоднозначно: выбери проект или укажи account_id'))
    raise ApiError(404, _ui_text('аккаунта нет в выбранном проекте или среди твоих агентов без проекта'))


# ---------------------------------------------------------------- projects
# A project is one team's chat: its own feed, board, claims and results, and its own members.
# People (owner | member) may be in many projects; an agent belongs to exactly one.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


def slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")[:40]
    return s if SLUG_RE.match(s) else "project"


def create_project_row(con, slug, title, by, creator_unknown=False):
    base, n = slug, 2
    while con.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base[:36]}-{n}"
        n += 1
    return con.execute("INSERT INTO projects(slug, title, created_by, created_ts, creator_unknown) VALUES (?,?,?,?,?)",
                       (slug, title[:80], by, time.time(), int(creator_unknown))).lastrowid


def add_member(con, pid, name, role, by):
    u = con.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
    special = con.execute("SELECT admin_owner,service_agent FROM projects WHERE id=?", (pid,)).fetchone()
    if special["admin_owner"] and name not in (special["admin_owner"], special["service_agent"]):
        raise ApiError(403, _ui_text('это личный чат администратора с привязанной сессией разработки'))
    check_member_name(con, pid, u["display_name"] or u["name"], name)
    if u["kind"] == "agent":
        if not member_role(pid, u["owner"]):
            raise ApiError(400, _ui_text('человек агента должен состоять в этом проекте'))
        role = "agent"
        other = con.execute("SELECT p.slug FROM members m JOIN projects p ON p.id = m.project_id "
                            "WHERE m.user = ? AND m.project_id != ?", (name, pid)).fetchone()
        if other:
            raise ApiError(409, _ui_text('этот аккаунт агента уже состоит в другом проекте'))
    con.execute("INSERT OR REPLACE INTO members(project_id, user, role, added_by, added_ts) VALUES (?,?,?,?,?)",
                (pid, name, role, by, time.time()))


def ensure_default_project(con):
    """A database from before projects: everything moves into one project, with all users as members."""
    if con.execute("SELECT 1 FROM projects LIMIT 1").fetchone() or not con.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return
    pid = create_project_row(con, Cfg.default_project["slug"], Cfg.default_project["title"], "migration", creator_unknown=True)
    users = con.execute("SELECT * FROM users WHERE revoked = 0 ORDER BY created_ts").fetchall()
    users = sorted(users, key=lambda u: u["kind"] != "human")
    owners = [u["name"] for u in users if u["kind"] == "human" and u["is_admin"]] or \
             [u["name"] for u in users if u["kind"] == "human"][:1]
    for u in users:
        add_member(con, pid, u["name"], "owner" if u["name"] in owners else "member", "migration")
    for table in ("messages", "claims"):
        con.execute(f"UPDATE {table} SET project_id = ? WHERE project_id IS NULL", (pid,))
    con.execute("UPDATE board SET project_id = ? WHERE project_id = 0", (pid,))
    con.execute("UPDATE invites SET project_id = ? WHERE project_id IS NULL AND purpose = 'new'", (pid,))


def my_projects(name):
    rows = db().execute("SELECT p.*, m.role FROM members m JOIN projects p ON p.id = m.project_id "
                        "WHERE m.user = ? AND p.archived = 0 ORDER BY m.sidebar_position IS NULL, m.sidebar_position, p.id", (name,)).fetchall()
    return [p for p in rows if member_role(p["id"], name)]


def member_role(pid, name):
    r = db().execute("SELECT m.role,p.admin_owner,p.service_agent,u.kind,u.owner,u.is_admin,u.revoked "
                     "FROM members m JOIN projects p ON p.id=m.project_id JOIN users u ON u.name=m.user "
                     "WHERE m.project_id=? AND m.user=?", (pid, name)).fetchone()
    if not r or r["revoked"]:
        return None
    if r["admin_owner"]:
        admin = db().execute("SELECT 1 FROM users WHERE name=? AND kind='human' AND is_admin=1 AND revoked=0",
                             (r["admin_owner"],)).fetchone()
        if not admin or not ((name == r["admin_owner"] and r["kind"] == "human" and r["is_admin"])
                             or (name == r["service_agent"] and r["kind"] == "agent" and r["owner"] == r["admin_owner"])):
            return None
    return r["role"]


def project_for(h, user, q):
    """The project a request is about: ?project= / X-Telegent-Project, or the only one the user is in."""
    slug = str(q.get("project") or h.headers.get("X-Telegent-Project") or "").strip()
    with DB_LOCK:
        rows = my_projects(user["name"])
    if slug:
        p = next((r for r in rows if r["slug"] == slug), None)
        if not p:
            raise ApiError(404, f"{_ui_text('проекта «')}{slug}{_ui_text('» нет или ты в нём не состоишь')}")
        return p
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise ApiError(409, _ui_text('ты пока не состоишь ни в одном проекте: создай проект или попроси приглашение'))
    raise ApiError(400, "ты в нескольких проектах, укажи какой: " + ", ".join(r["slug"] for r in rows) +
                        " (tg.py --project <имя> или \"project\" в конфиге)")


def token_hash(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


# ---------------------------------------------------------------- people: passwords and sessions
# Agents use tokens from files. People may also log in with name + password: the server keeps only a
# PBKDF2 hash, and a login hands out a session token (tgs_...) that works like a token until logout.
PW_ITERATIONS = 120_000
PW_MIN_LEN = 8
LOGIN_WINDOW = 24 * 3600                 # failures are remembered for a day
LOGIN_MAX_FAILS = {"name": 5, "ip": 20}  # free tries; then a pause after every failure:
LOGIN_LOCK = 20                          # 20 s, 40 s, 80 s, ... doubling,
LOGIN_LOCK_MAX = 24 * 3600               # up to a day
_login_fails = {}


def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PW_ITERATIONS)
    return f"pbkdf2_sha256${PW_ITERATIONS}${salt.hex()}${dk.hex()}"


def check_password(pw, stored):
    try:
        algo, iters, salt, dk = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return algo == "pbkdf2_sha256" and secrets.compare_digest(got.hex(), dk)
    except (ValueError, AttributeError):
        return False


def lock_seconds(n_fails, kind):
    over = n_fails - LOGIN_MAX_FAILS[kind]
    return 0 if over < 0 else min(LOGIN_LOCK * 2 ** over, LOGIN_LOCK_MAX)


def human_wait(sec):
    sec = max(1, -int(-sec // 1))   # ceil
    if sec < 120:
        return f"{sec} с"
    if sec < 2 * 3600:
        return f"{-int(-sec // 60)} мин"
    return f"{-int(-sec // 3600)} ч"


def login_throttle(keys):
    """Raise 429 while a key (name:..., ip:...) is paused: after LOGIN_MAX_FAILS failures every further
    failure pauses it, 20 s, 40 s, 80 s, ... up to a day. The right password or a reset link clears it."""
    now = time.time()
    for key in keys:
        fails = [t for t in _login_fails.get(key, []) if now - t < LOGIN_WINDOW]
        _login_fails[key] = fails
        lock = lock_seconds(len(fails), key.split(":")[0])
        if lock and now - fails[-1] < lock:
            raise ApiError(429, f"{_ui_text('много неверных попыток, подожди ')}{human_wait(lock - (now - fails[-1]))}{_ui_text(' и попробуй ещё. Забыл пароль — попроси ссылку для сброса')}")


def login_failed(keys):
    for key in keys:
        _login_fails.setdefault(key, []).append(time.time())


def new_session(user_name, label=""):
    tok = "tgs_" + secrets.token_hex(24)
    with DB_LOCK:
        db().execute("INSERT INTO sessions(token_hash, user, created_ts, last_ts, label) VALUES (?,?,?,?,?)",
                     (token_hash(tok), user_name, time.time(), time.time(), label[:120]))
    return tok


# ---------------------------------------------------------------- invites
# One-time links (7 days), always for a given name.
#   new   - a new person (picks a password) or a new agent (gets a token via `tg.py join`), straight into a project
#   add   - an existing account joins a project; it has to be logged in as that name to accept
#   reset - a new password (person) or a new token (agent); the old one stops working; no project
INVITE_DAYS = 7


def create_invite(by, name, kind=None, owner=None, pid=None, reset=False, account_id=None):
    if not NAME_RE.fullmatch(name or ""):
        raise ApiError(400, _ui_text('имя: латиница, цифры, . _ -, до 32 символов'))
    with DB_LOCK:
        con = db()
        if pid and con.execute("SELECT 1 FROM projects WHERE id=? AND admin_owner IS NOT NULL", (pid,)).fetchone():
            raise ApiError(403, _ui_text('в личный чат администратора не приглашают других участников'))
        if reset:
            u = resolve_account(name, account_id=account_id)
        elif account_id:
            u = resolve_account(account_id=account_id)
        else:
            local = find_accounts(name, pid) if pid is not None else []
            humans = find_accounts(name, kind="human") if kind != "agent" else []
            u = (local or humans or [None])[0]
        if u:
            name = account_name(u["name"])
            if u["revoked"]:
                raise ApiError(400, _ui_text('аккаунт отозван; создай другой аккаунт'))
            if not reset and kind and kind != u["kind"]:
                raise ApiError(409, _ui_text('это имя уже занято в проекте'))
        role = None
        if reset:
            kind, owner, purpose, pid = u["kind"], u["owner"], "reset", None
        elif u:
            kind, owner, purpose = u["kind"], u["owner"], "add"
            if member_role(pid, u["name"]):
                raise ApiError(409, _ui_text('аккаунт уже в этом проекте'))
            if kind == "agent" and con.execute("SELECT 1 FROM members WHERE user=?", (u["name"],)).fetchone():
                raise ApiError(409, _ui_text('этот аккаунт агента уже состоит в другом проекте'))
            check_member_name(con, pid, name)
            role = "agent" if kind == "agent" else "member"
        else:
            kind = kind or "human"
            if kind not in ("human", "agent"):
                raise ApiError(400, "kind: human | agent")
            check_member_name(con, pid, name)
            owner, role = (name, "member") if kind == "human" else (owner or by, "agent")
            purpose = "new"
        if kind == "agent" and not reset:
            person = con.execute("SELECT * FROM users WHERE name=? AND kind='human' AND revoked=0", (owner,)).fetchone()
            if not person or not member_role(pid, owner):
                raise ApiError(400, _ui_text('владелец агента должен быть человеком из этого проекта'))
        code = "tgi_" + secrets.token_hex(24)
        now = time.time()
        target = u["name"] if u else None
        if reset:
            con.execute("DELETE FROM invites WHERE purpose='reset' AND account_id=? AND used_ts IS NULL", (target,))
        else:
            con.execute("DELETE FROM invites WHERE lower(name)=lower(?) AND project_id=? "
                        "AND purpose!='reset' AND used_ts IS NULL", (name, pid))
        con.execute("INSERT INTO invites(code_hash,name,kind,owner,purpose,created_by,created_ts,expires_ts,project_id,role,account_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (token_hash(code), name, kind, owner, purpose, by, now,
                                                     now+INVITE_DAYS*86400, pid, role, target))
    return {"code": code, "name": name, "kind": kind, "owner": account_name(owner), "account_id": target,
            "purpose": purpose, "expires": iso(now+INVITE_DAYS*86400)}


def live_invite(code):
    with DB_LOCK:
        inv = db().execute("SELECT i.*, p.slug AS project_slug, p.title AS project_title FROM invites i "
                           "LEFT JOIN projects p ON p.id = i.project_id WHERE i.code_hash = ?",
                           (token_hash(code or ""),)).fetchone()
    if not inv:
        raise ApiError(404, _ui_text('приглашение не найдено: ссылка неверная или её заменили новой'))
    if inv["used_ts"]:
        raise ApiError(410, _ui_text('приглашение уже использовано; если это был не ты, скажи тому, кто приглашал'))
    if inv["expires_ts"] < time.time():
        raise ApiError(410, _ui_text('приглашение просрочено; попроси новое'))
    return inv


def redeem_invite(code, password=None, label="", me=None, as_existing=False):
    tok = "tgt_" + secrets.token_hex(24)
    with DB_LOCK:
        con = db()
        con.execute("BEGIN IMMEDIATE")
        try:
            inv = live_invite(code)
            name, kind, purpose = inv["name"], inv["kind"], inv["purpose"]
            key = inv["account_id"]
            if kind == "agent" and purpose != "reset":
                owner = con.execute("SELECT 1 FROM users WHERE name=? AND kind='human' AND revoked=0", (inv["owner"],)).fetchone()
                if not owner or not member_role(inv["project_id"], inv["owner"]):
                    raise ApiError(410, _ui_text('владелец агента больше не состоит в проекте; попроси новое приглашение'))
            if as_existing:
                if purpose != "new" or kind != "human":
                    raise ApiError(400, _ui_text('так можно принять только приглашение нового человека'))
                if not me or me["kind"] != "human":
                    raise ApiError(401, _ui_text('войди в свой аккаунт'))
                key, name, purpose = me["name"], account_name(me["name"]), "add"
            elif purpose == "add":
                if not me or me["name"] != key:
                    raise ApiError(401, f"{_ui_text('это приглашение для ')}{name}{_ui_text(': войди своим аккаунтом')}")
            elif kind == "human" and len(password or "") < PW_MIN_LEN:
                raise ApiError(400, f"{_ui_text('придумай пароль от ')}{PW_MIN_LEN}{_ui_text(' символов')}")
            pw = hash_password(password) if kind == "human" and purpose != "add" else None
            if purpose == "new":
                check_member_name(con, inv["project_id"], name)
                if kind == "human" and find_accounts(name, kind="human"):
                    raise ApiError(409, _ui_text('имя человека уже занято: войди существующим аккаунтом'))
                key = new_account(con, name, kind, inv["owner"], tok, pw)
                add_member(con, inv["project_id"], key, inv["role"], inv["created_by"])
            elif purpose == "add":
                if member_role(inv["project_id"], key):
                    raise ApiError(409, _ui_text('аккаунт уже в этом проекте'))
                add_member(con, inv["project_id"], key, "member" if as_existing else inv["role"], inv["created_by"])
            else:
                target = con.execute("SELECT * FROM users WHERE name=? AND revoked=0", (key,)).fetchone()
                if not target or target["kind"] != kind:
                    raise ApiError(410, _ui_text('аккаунт приглашения больше недоступен'))
                name = account_name(key)
                if kind == "human":
                    con.execute("UPDATE users SET pw_hash=? WHERE name=?", (pw, key))
                    con.execute("DELETE FROM sessions WHERE user=?", (key,))
                    _login_fails.pop(f"name:{name.lower()}", None)
                else:
                    con.execute("UPDATE users SET token_hash=? WHERE name=?", (token_hash(tok), key))
            con.execute("UPDATE invites SET used_ts=? WHERE code_hash=?", (time.time(), inv["code_hash"]))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    res = {"id": key, "name": name, "kind": kind, "purpose": purpose, "project": inv["project_slug"]}
    if purpose != "add":
        res["token"] = new_session(key, label) if kind == "human" else tok
    return res


# UI language affects exception text only; protocol values and stored messages stay unchanged.
_UI_CONTEXT = threading.local()
_UI_EN = {'активность сообщает клиент агента': 'Activity is reported by the agent client',
 'активность: state working/idle и непрозрачная метка клиента': 'Activity requires state working/idle and an opaque client id',
 'аккаунта нет в выбранном проекте или среди твоих агентов без проекта': 'Account not found in the selected '
                                                                         'project or among your agents '
                                                                         'without a project',
 'имя и account_id должны быть строками': 'name and account_id must be strings',
 'аккаунта нет в выбранном проекте или доступном списке': 'Account not found in the selected project or '
                                                          'available list',
 'имя неоднозначно: выбери проект или укажи account_id': 'Name is ambiguous: choose a project or specify '
                                                         'account_id',
 'это имя уже занято в проекте': 'This name is already taken in the project',
 'это личный чат администратора с привязанной сессией разработки': 'This is the administrator’s direct chat '
                                                                   'with the linked development session',
 'ты пока не состоишь ни в одном проекте: создай проект или попроси приглашение': 'You’re not in a project '
                                                                                  'yet. Create one or ask '
                                                                                  'for an invitation',
 'имя: латиница, цифры, . _ -, до 32 символов': 'name: Latin letters, digits, . _ -, up to 32 characters',
 'приглашение не найдено: ссылка неверная или её заменили новой': 'Invitation not found: the link is invalid '
                                                                  'or has been replaced',
 'приглашение уже использовано; если это был не ты, скажи тому, кто приглашал': 'Invitation already used. If '
                                                                                'you didn’t use it, tell the '
                                                                                'person who invited you',
 'приглашение просрочено; попроси новое': 'Invitation expired. Ask for a new one',
 'похоже на секрет (': 'Looks like a secret (',
 ') в поле «': ') in field “',
 '», строка ': '”, line ',
 '. Сообщение НЕ отправлено. Убери значение (напиши «см. файл X у меня») и пошли снова.': '. Message NOT '
                                                                                          'sent. Remove the '
                                                                                          'value (write “see '
                                                                                          'file X on my '
                                                                                          'machine”) and try '
                                                                                          'again.',
 'повреждён ключ хранилища секретов': 'The secret storage key is corrupted',
 'нужен прежний ключ хранилища секретов': 'The previous secret storage key is required',
 'нет такого личного чата': 'Direct chat not found',
 'type должен быть одним из: ': 'type must be one of: ',
 'текст длиннее ': 'Message is longer than ',
 ' символов — положи его во вложение': ' characters. Attach it as a file instead',
 'пустое сообщение': 'Message cannot be empty',
 'адресаты: список имён или to_ids': 'recipients: a list of names or to_ids',
 'attachments: список, не больше 30 вложений': 'attachments: must be a list of no more than 30 items',
 'не больше 30 вложений': 'No more than 30 attachments are allowed',
 'один файл нельзя прикрепить дважды': 'The same file cannot be attached twice',
 'сообщения #': 'Message #',
 ' нет': ' not found',
 'нет доступа к проекту': 'You don’t have access to this project',
 'статус есть только у ': 'Only ',
 'одобрять может только человек': 'Only a person can approve',
 ' не просит одобрения': ' does not request approval',
 'решение по #': 'Decision on #',
 ' принимает только ': ' can only be made by ',
 'чат с разработчиком доступен только администратору сервера': 'The developer chat is available only to the '
                                                               'server administrator',
 'название проекта должно быть текстом': 'Project name must be text',
 'нужно название проекта': 'Project name is required',
 'название проекта: не больше 80 символов': 'Project name must be no longer than 80 characters',
 'название проекта должно быть одной строкой без управляющих символов': 'Project name must be a single line '
                                                                        'without control characters',
 ' ещё нет пароля: войди один раз по ссылке с токеном и задай его (кнопка «Пароль»)': ' has no password yet. '
                                                                                      'Sign in once with the '
                                                                                      'token link and set '
                                                                                      'one using the '
                                                                                      '“Password” button',
 'неверное имя или пароль': 'Incorrect name or password',
 'пароль бывает только у людей; агенты входят по токену': 'Only people use passwords; agents sign in with a '
                                                          'token',
 'пароль короче ': 'Password must be at least ',
 ' символов': ' characters',
 'это может только владелец проекта «': 'Only the owner of project “',
 'приглашает и добавляет участников только человек': 'Only a person can invite and add members',
 'порядок проектов выбирает человек': 'Only a person can choose the project order',
 'project_ids: список разных идентификаторов проектов': 'project_ids: must be a list of distinct project IDs',
 'проекты создают люди, не агенты': 'Projects can be created by people, not agents',
 'короткое имя проекта: латиница, цифры и -, от 2 до 40 символов': 'Project slug: Latin letters, digits, and '
                                                                   'hyphens; 2 to 40 characters',
 'для подтверждения введи название проекта: ': 'To confirm, enter the project name: ',
 'переименовывает человек: себя или своих агентов': 'Only a person can rename themselves or their own agents',
 'сообщение удалено': 'Message was deleted',
 'загрузка не найдена': 'Upload not found',
 'Недостаточно места на сервере для файла: свободно ': 'Not enough server space for this file: ',
 ' МБ. Нужно сохранить не менее 300 МБ свободными. Можно отправить текст без вложения.': ' MB free. At least '
                                                                                         '300 MB must remain '
                                                                                         'available. You can '
                                                                                         'send the message '
                                                                                         'without an '
                                                                                         'attachment.',
 'максимальный размер одного файла — 200 МБ': 'Maximum file size is 200 MB',
 'md5: ожидаются 32 шестнадцатеричных символа': 'md5: expected 32 hexadecimal characters',
 'часть файла должна быть от 1 байта до 4 МБ': 'File chunk must be between 1 byte and 4 MB',
 'часть файла получена не полностью — повтори загрузку': 'File chunk was incomplete. Retry the upload',
 'нет такого вложения': 'Attachment not found',
 'вложение по ссылке: ': 'Linked attachment: ',
 'нужен key — что именно занимаешь': 'key is required: specify what you’re claiming',
 'hours должно быть больше 0 и не больше года': 'hours must be greater than 0 and no more than one year',
 'недопустимое имя агента': 'Invalid agent name',
 'имя неоднозначно: укажи --project или --account-id': 'Name is ambiguous: specify --project or --account-id',
 'человек агента должен состоять в этом проекте': 'The agent’s person must be a member of this project',
 'этот аккаунт агента уже состоит в другом проекте': 'This agent account already belongs to another project',
 'проекта «': 'Project “',
 '» нет или ты в нём не состоишь': '” does not exist or you’re not a member',
 'много неверных попыток, подожди ': 'Too many failed attempts. Wait ',
 ' и попробуй ещё. Забыл пароль — попроси ссылку для сброса': ' and try again. Forgot your password? Ask for '
                                                              'a reset link',
 'в личный чат администратора не приглашают других участников': 'Other members cannot be invited to the '
                                                                'administrator’s direct chat',
 'передача секретов недоступна: на сервере не найдена системная libcrypto': 'Secret transfer is unavailable: '
                                                                            'system libcrypto was not found '
                                                                            'on the server',
 'повреждены параметры хранилища секретов': 'Secret storage settings are corrupted',
 'не удалось открыть хранилище секретов': 'Could not open secret storage',
 'ключ хранилища отсутствует: восстанови его из резервной копии': 'Storage key is missing. Restore it from a '
                                                                  'backup',
 'ключ хранилища недоступен: восстанови его из резервной копии': 'Storage key is unavailable. Restore it '
                                                                 'from a backup',
 'ответ должен оставаться в исходном чате': 'A reply must stay in the original chat',
 'за этим именем теперь другой аккаунт: выбери адресата по to_ids': 'A different account now uses this name. '
                                                                    'Choose the recipient with to_ids',
 'result должен быть объектом': 'result must be an object',
 'одобряющий должен быть человеком из этого проекта': 'The approver must be a person in this project',
 'в личном чате только два участника': 'A direct chat can have only two participants',
 'заявки проекта публикуются в общем чате': 'Project claims must be posted in the team chat',
 'текст сообщения изменился; прочитай актуальную версию перед решением': 'The message changed. Read the '
                                                                         'latest version before deciding',
 'нужен токен (Authorization: Bearer ...)': 'Token required (Authorization: Bearer ...)',
 'неизвестный или отозванный токен (или сессия закрыта)': 'Unknown or revoked token (or the session has '
                                                          'ended)',
 'запрос слишком большой': 'Request is too large',
 'текущий пароль неверный': 'Current password is incorrect',
 'сбросить можно токен своего агента; пароль человека сбрасывает админ сервера': 'You can reset your own '
                                                                                 'agent’s token; a person’s '
                                                                                 'password can be reset by '
                                                                                 'the server administrator',
 'проект не найден': 'Project not found',
 'переименовать проект может только его создатель — человек из этого проекта': 'Only the project creator, '
                                                                               'who must be a member, can '
                                                                               'rename it',
 'имя неоднозначно: выбери аккаунт по account_id': 'Name is ambiguous. Select an account with account_id',
 'это последний владелец: сначала сделай владельцем кого-то ещё': 'This is the last owner. Assign someone '
                                                                  'else as owner first',
 'переименовать можно себя и своих агентов': 'You can rename yourself and your own agents',
 'имя человека уже занято': 'This person’s name is already taken',
 'имя занято действующим приглашением': 'This name is reserved by an active invitation',
 'это последний владелец': 'This is the last owner',
 'нет такого участника': 'Member not found',
 'личный чат связывает человека и агента своего проекта': 'A direct chat connects a person with an agent in '
                                                          'their project',
 'секрет адресуется агенту от человека личного чата': 'A secret can be sent to the agent by the person in '
                                                      'the direct chat',
 'нужен уникальный idem_key от 16 до 100 символов': 'A unique idem_key of 16 to 100 characters is required',
 'секрет: непустой текст до 32 КиБ': 'secret: non-empty text up to 32 KiB',
 'срок секрета: от 5 минут до суток': 'secret lifetime: 5 minutes to 24 hours',
 'нет такого секрета': 'Secret not found',
 'получить секрет может только адресованный агент': 'Only the addressed agent can retrieve this secret',
 'нужен постоянный claim_key из 64 шестнадцатеричных знаков для безопасного повтора': 'A persistent '
                                                                                      '64-character '
                                                                                      'hexadecimal claim_key '
                                                                                      'is required for safe '
                                                                                      'retries',
 'секрет получен, отозван или срок его действия истёк': 'Secret was retrieved, revoked, or has expired',
 'получение уже начато: продолжи прежнюю команду на том же устройстве': 'Retrieval has already started. '
                                                                        'Continue with the original command '
                                                                        'on the same device',
 'нет сообщения в выбранном проекте': 'Message not found in the selected project',
 'человек может изменить или удалить только собственное сообщение': 'A person can edit or delete only their '
                                                                    'own messages',
 'нужны текущая revision и текст body для изменения': 'Current revision and body text are required for '
                                                      'editing',
 'нужны emoji и active (true или false)': 'emoji and active (true or false) are required',
 'реакция: ❤️, 🔥, 👍, 👎, 🤡, 😁, 😭; active: true | false': 'reaction: ❤️, 🔥, 👍, 👎, 🤡, 😁, 😭; active: true | false',
 'реакции доступны у сообщений участников': 'Reactions are available on member messages',
 'нужны Content-Length и offset': 'Content-Length and offset are required',
 'загрузка уже завершена': 'Upload is already complete',
 'неверная позиция загрузки': 'Invalid upload offset',
 'файл получен не полностью': 'File was not fully received',
 'отправленный файл удаляется только вместе с проектом': 'A sent file can only be deleted along with its '
                                                         'project',
 'нажми «Скачать» у файла в чате': 'Click “Download” on the file in chat',
 'ссылка на скачивание истекла; нажми «Скачать» ещё раз': 'Download link expired. Click “Download” again',
 'вход больше не действителен': 'Your sign-in is no longer valid',
 'hours должно быть числом (часы, можно дробное)': 'hours must be a number (hours; fractions are allowed)',
 'аккаунт отозван; создай другой аккаунт': 'Account was revoked. Create another account',
 'владелец агента должен быть человеком из этого проекта': 'The agent’s owner must be a person in this '
                                                           'project',
 'системная libcrypto не поддерживает AES-GCM': 'System libcrypto does not support AES-GCM',
 'не удалось проверить или зашифровать секрет': 'Could not validate or encrypt the secret',
 'ключ сообщения уже использован в другом чате': 'Message key has already been used in another chat',
 'нет таких адресатов в выбранном проекте': 'No such recipients in the selected project',
 'файл ещё не загружен полностью или уже отправлен': 'File upload is incomplete or the file has already been '
                                                     'sent',
 'участник может пригласить своего агента; людей приглашает владелец проекта': 'A member can invite their '
                                                                               'own agent; only the project '
                                                                               'owner can invite people',
 'ключ сообщения использован в другом сообщении': 'Message key was used by another message',
 'отозвать секрет может отправитель': 'Only the sender can revoke the secret',
 'секрет уже получен: отзыв не удалит его у получателя': 'Secret has already been retrieved; revoking it '
                                                         'won’t remove it from the recipient',
 'получение уже началось: отзыв не удалит файл у получателя': 'Retrieval has already started; revoking it '
                                                              'won’t remove the recipient’s file',
 'сначала получи пакет': 'Retrieve the package first',
 'карточка секрета не редактируется': 'Secret cards cannot be edited',
 'body: текст сообщения': 'body: message text',
 'текст слишком длинный': 'Message is too long',
 'сообщение уже изменилось; закрой редактор и открой актуальную версию': 'Message has changed. Close the '
                                                                         'editor and open the latest version',
 'файл изменился во время отправки или повреждён; отправь его заново': 'File changed during upload or is '
                                                                       'corrupted. Send it again',
 'некорректный диапазон файла': 'Invalid file range',
 'диапазон выходит за размер файла': 'Range exceeds file size',
 'нужен key или id': 'key or id is required',
 'чужую заявку снимает только человек': 'Only a person can release someone else’s claim',
 'аккаунт уже в этом проекте': 'Account is already in this project',
 'владелец агента больше не состоит в проекте; попроси новое приглашение': 'The agent’s owner is no longer '
                                                                           'in the project. Ask for a new '
                                                                           'invitation',
 'так можно принять только приглашение нового человека': 'Only an invitation for a new person can be '
                                                         'accepted this way',
 'войди в свой аккаунт': 'Sign in to your account',
 'имя человека уже занято: войди существующим аккаунтом': 'This person’s name is already taken. Sign in with '
                                                          'the existing account',
 'текст содержит недопустимый Unicode': 'Message contains invalid Unicode',
 'ключ загрузки уже занят другим файлом': 'Upload key is already in use by another file',
 'повторная часть отличается от полученной': 'Retry chunk differs from the chunk already received',
 'id должен быть числом': 'id must be a number',
 'активной заявки «': 'Active claim “',
 '» нет': '” not found',
 'аккаунт сессии отозван; сначала реши это штатным управлением аккаунтами': 'Session account was revoked. '
                                                                            'Resolve this using account '
                                                                            'management first',
 'файл токена уже существует: выбери другой путь': 'Token file already exists. Choose another path',
 'имя уже есть в этой области; --rotate выдаст новый токен': 'Name already exists in this scope; --rotate '
                                                             'will issue a new token',
 'это приглашение для ': 'This invitation is for ',
 ': войди своим аккаунтом': ': sign in with that account',
 'придумай пароль от ': 'Create a password of at least ',
 'аккаунт приглашения больше недоступен': 'The invitation account is no longer available',
 'На сервере закончилось место. Отправь текст без вложения.': 'The server is out of space. Send the message '
                                                              'without an attachment',
 'агент уже привязан; нужен его прежний файл токена или явный --rotate': 'Agent is already linked. Use its '
                                                                         'existing token file or specify '
                                                                         '--rotate',
 'нет такого пути': 'Path not found',
 'напрямую добавляются только люди из твоих общих проектов и твои агенты без проекта; ': 'You can add only '
                                                                                         'people from '
                                                                                         'projects you share '
                                                                                         'and your own '
                                                                                         'agents without a '
                                                                                         'project; ',
 ' пригласи ссылкой': ' invite them with a link',
 ' нет в проекте': ' is not in the project',
 ': строка или число до 500 символов': ': must be a string or number up to 500 characters',
 'некорректный Content-Length': 'Invalid Content-Length',
 'тело запроса — не JSON': 'Request body is not JSON',
 'seen_upto: целый неотрицательный номер, только вместе с kind=read': 'seen_upto: a non-negative integer, '
                                                                      'only with kind=read',
 '» уже занято: ': '” is already claimed by ',
 'заявки #': 'Claim #',
 'заявка #': 'Claim #',
 '» уже снята': '” has already been released',
 '» занял ': '” is claimed by ',
 ': чужую заявку снимает только человек': ': only a person can release someone else’s claim',
 'вложение ': 'Attachment ',
 ' больше ': ' exceeds ',
 ' байт — пришли ссылкой и md5': ' bytes. Send it as a link with its md5',
 ': нужен content_b64 или link': ': content_b64 or link is required',
 ': битый base64': ': invalid base64',
 'у ': 'For '}
def _ui_pattern_key(key):
    boundary = r"[A-Za-zА-Яа-яЁё0-9_]"
    return ("(?<!" + boundary + ")" if key[0].isalnum() else "") + re.escape(key) + ("(?!" + boundary + ")" if key[-1].isalnum() else "")
_UI_PATTERN = re.compile("|".join(_ui_pattern_key(key) for key in sorted(_UI_EN, key=len, reverse=True)))

def _ui_text(text):
    if getattr(_UI_CONTEXT, "language", "ru") != "en":
        return text
    return _UI_PATTERN.sub(lambda match: _UI_EN[match.group()], text)


class ApiError(Exception):
    def __init__(self, code, msg, **extra):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.extra = extra


def text_encoding_problem(text):
    # ASCII replacement happens upstream (notably PowerShell 5.1); decoding cannot restore it.
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        return "служебные символы вместо текста: проверь кодировку источника"
    if text.count("\ufffd") >= 3:
        return "символы замены Unicode: часть исходного текста уже потеряна"
    runs = re.findall(r"\?{3,}", text)
    ratio=text.count("?") / max(1,len(text))
    if (text.count("?") >= 12 and ratio >= .15 and (len(runs) >= 3 or any(len(r) >= 12 for r in runs))) or (text.count("?") >= 6 and ratio >= .4 and any(len(r)>=4 for r in runs)):
        return "слишком много вопросительных знаков: возможно, буквы заменены до запуска клиента"
    return None



def check_secret(field, text):
    hit = find_secret(text, Cfg.deny_literals)
    if hit:
        raise ApiError(422, f"{_ui_text('похоже на секрет (')}{hit[0]}{_ui_text(') в поле «')}{field}{_ui_text('», строка ')}{hit[1]}{_ui_text('. Сообщение НЕ отправлено. Убери значение (напиши «см. файл X у меня») и пошли снова.')}")


# ---------------------------------------------------------------- presence
# A client "listens" while it keeps a /api/wait long-poll going (tg.py watch, the web page).
LISTEN_WINDOW = 75
_touched = {}
ACTIVITY_WINDOW = 180
_agent_activity = {}
_activity_lock = threading.Lock()


def agent_working(name, now=None):
    now = time.time() if now is None else now
    with _activity_lock:
        clients = {k:v for k,v in _agent_activity.get(name, {}).items() if 0 <= now-v[1] < ACTIVITY_WINDOW}
        if clients:
            _agent_activity[name] = clients
            return any(v[0] == "working" for v in clients.values())
        _agent_activity.pop(name, None)
    return None


def touch(user, waiting=False, checking=False):
    now = time.time()
    key = (user["name"], "check" if checking else waiting)
    if now - _touched.get(key, 0) < 15:
        return
    _touched[key] = now
    with DB_LOCK:
        if checking:
            db().execute("UPDATE users SET last_seen_ts = ?, last_check_ts = ? WHERE name = ?", (now, now, user["name"]))
        elif waiting:
            db().execute("UPDATE users SET last_seen_ts = ?, last_wait_ts = ? WHERE name = ?", (now, now, user["name"]))
        else:
            db().execute("UPDATE users SET last_seen_ts = ? WHERE name = ?", (now, user["name"]))


def presence(row, now=None):
    now = now or time.time()
    lw = row["last_wait_ts"]
    return {"listening": bool(lw and now - lw < LISTEN_WINDOW), "last_listen": iso(lw),
            "working": agent_working(row["name"], now) if row["kind"] == "agent" else None,
            "last_check": iso(row["last_check_ts"]), "last_seen": iso(row["last_seen_ts"])}


def users_with_presence(pid):
    """Members of a project (only them: other projects' people stay invisible) with presence and role."""
    now = time.time()
    return [dict(account_dict(r), role=r["role"], **presence(r, now))
            for r in db().execute("SELECT u.*, m.role FROM members m JOIN users u ON u.name = m.user "
                                  "WHERE m.project_id = ? AND u.revoked = 0 ORDER BY u.kind, u.name", (pid,))
            if member_role(pid, r["name"])]


# ---------------------------------------------------------------- message helpers
class SecretCipher:
    """AES-GCM through the installed OpenSSL, not a Python implementation of a cipher."""
    def __init__(self):
        candidates = [os.path.join(sys.base_prefix, "DLLs", "libcrypto-3.dll"),
                      os.path.join(sys.base_prefix, "DLLs", "libcrypto-1_1.dll")] if os.name == "nt" else [ctypes.util.find_library("crypto")]
        self.lib = None
        for path in candidates:
            if path and (os.name != "nt" or os.path.isfile(path)):
                try:
                    self.lib = ctypes.CDLL(path)
                    break
                except OSError:
                    pass
        if not self.lib:
            raise ApiError(503, _ui_text('передача секретов недоступна: на сервере не найдена системная libcrypto'))
        p, i = ctypes.c_void_p, ctypes.c_int
        signatures = {"EVP_CIPHER_CTX_new": (p, []), "EVP_CIPHER_CTX_free": (None, [p]),
                      "EVP_aes_256_gcm": (p, []), "EVP_CIPHER_CTX_ctrl": (i, [p,i,i,p])}
        for prefix in ("Encrypt", "Decrypt"):
            signatures["EVP_"+prefix+"Init_ex"] = (i, [p,p,p,p,p])
            signatures["EVP_"+prefix+"Update"] = (i, [p,p,ctypes.POINTER(i),p,i])
            signatures["EVP_"+prefix+"Final_ex"] = (i, [p,p,ctypes.POINTER(i)])
        try:
            for name, (result, args) in signatures.items():
                f = getattr(self.lib, name); f.restype = result; f.argtypes = args
        except AttributeError:
            raise ApiError(503, _ui_text('системная libcrypto не поддерживает AES-GCM'))

    def crypt(self, key, iv, value, aad, tag=None):
        if len(key) != 32 or len(iv) != 12 or (tag is not None and len(tag) != 16):
            raise ApiError(503, _ui_text('повреждены параметры хранилища секретов'))
        lib = self.lib; ctx = lib.EVP_CIPHER_CTX_new()
        if not ctx:
            raise ApiError(503, _ui_text('не удалось открыть хранилище секретов'))
        prefix = "Decrypt" if tag is not None else "Encrypt"
        def check(value):
            if value != 1:
                raise ApiError(503, _ui_text('не удалось проверить или зашифровать секрет'))
        try:
            check(getattr(lib, "EVP_"+prefix+"Init_ex")(ctx,lib.EVP_aes_256_gcm(),None,key,iv))
            n = ctypes.c_int()
            update = getattr(lib, "EVP_"+prefix+"Update")
            check(update(ctx,None,ctypes.byref(n),aad,len(aad)))
            out = ctypes.create_string_buffer(len(value)+16)
            check(update(ctx,out,ctypes.byref(n),value,len(value))); used = n.value
            auth = ctypes.create_string_buffer(tag,16) if tag is not None else ctypes.create_string_buffer(16)
            if tag is not None:
                check(lib.EVP_CIPHER_CTX_ctrl(ctx,0x11,16,auth))
            check(getattr(lib, "EVP_"+prefix+"Final_ex")(ctx,ctypes.byref(out,used),ctypes.byref(n)))
            if tag is None:
                check(lib.EVP_CIPHER_CTX_ctrl(ctx,0x10,16,auth))
            return out.raw[:used+n.value], auth.raw
        finally:
            lib.EVP_CIPHER_CTX_free(ctx)


def storage_secret_key(create=False):
    path = os.path.abspath(Cfg.db)+".secret-key"
    if not os.path.isfile(path) and create:
        if db().execute("SELECT 1 FROM secret_envelopes WHERE envelope IS NOT NULL LIMIT 1").fetchone():
            raise ApiError(503, _ui_text('ключ хранилища отсутствует: восстанови его из резервной копии'))
        try:
            fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,"wb") as f:
                f.write(secrets.token_bytes(32)); f.flush(); os.fsync(f.fileno())
        except FileExistsError:
            pass
    try:
        with open(path,"rb") as f: key = f.read(33)
    except OSError:
        raise ApiError(503, _ui_text('ключ хранилища недоступен: восстанови его из резервной копии'))
    if len(key) != 32:
        raise ApiError(503, _ui_text('повреждён ключ хранилища секретов'))
    return key


def seal_secret(raw, aad):
    cipher = SecretCipher(); key = storage_secret_key(create=True); iv = secrets.token_bytes(12)
    encrypted, tag = cipher.crypt(key,iv,raw,aad)
    return {"key_id": hashlib.sha256(key).hexdigest(), "iv":base64.b64encode(iv).decode(),
            "ciphertext":base64.b64encode(encrypted).decode(), "tag":base64.b64encode(tag).decode()}


def unseal_secret(envelope, aad):
    key = storage_secret_key()
    if envelope["key_id"] != hashlib.sha256(key).hexdigest():
        raise ApiError(503, _ui_text('нужен прежний ключ хранилища секретов'))
    raw, _ = SecretCipher().crypt(key,base64.b64decode(envelope["iv"]),base64.b64decode(envelope["ciphertext"]),aad,
                                 base64.b64decode(envelope["tag"]))
    return raw


def purge_expired_secrets():
    with DB_LOCK:
        db().execute("UPDATE secret_envelopes SET envelope=NULL WHERE envelope IS NOT NULL AND expires<=?",(time.time(),))


def conversation_row(cid, user, pid=None):
    if not isinstance(cid,str) or not re.fullmatch(r"d_[0-9a-f]{48}",cid):
        raise ApiError(404, _ui_text('нет такого личного чата'))
    c = db().execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    if (not c or (pid is not None and c["project_id"] != pid) or user["name"] not in (c["human"], c["agent"])
            or not all(member_role(c["project_id"], key) for key in (c["human"], c["agent"]))):
        raise ApiError(404, _ui_text('нет такого личного чата'))
    return c


def can_read_message(row, user):
    if not row or not member_role(row["project_id"], user["name"]):
        return False
    if row["conversation_id"]:
        try:
            conversation_row(row["conversation_id"], user, row["project_id"])
        except ApiError:
            return False
    return True


def message_visibility(me, alias="m"):
    # Recheck both memberships on every request, including old messages after a member leaves.
    return (f"({alias}.conversation_id IS NULL OR EXISTS (SELECT 1 FROM conversations c "
            f"JOIN members ch ON ch.project_id=c.project_id AND ch.user=c.human "
            f"JOIN members ca ON ca.project_id=c.project_id AND ca.user=c.agent "
            f"JOIN users uh ON uh.name=c.human AND uh.revoked=0 "
            f"JOIN users ua ON ua.name=c.agent AND ua.revoked=0 "
            f"WHERE c.id={alias}.conversation_id AND c.project_id={alias}.project_id "
            "AND (c.human=? OR c.agent=?)))", [me, me])


def conversation_dict(c, user):
    peer = c["agent"] if user["name"] == c["human"] else c["human"]
    cursor = c["human_read_upto"] if user["name"] == c["human"] else c["agent_read_upto"]
    last = db().execute("SELECT id,body,ts,deleted_ts FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1", (c["id"],)).fetchone()
    unread = db().execute("SELECT COUNT(*) FROM messages m WHERE m.conversation_id=? AND m.id>? AND m.author!=? "
                          "AND m.deleted_ts IS NULL AND m.type NOT IN ('status','approval') AND NOT EXISTS (SELECT 1 FROM receipts r "
                          "WHERE r.message_id=m.id AND r.user=? AND r.read_ts IS NOT NULL)",
                          (c["id"], cursor, user["name"], user["name"])).fetchone()[0]
    return {"id": c["id"], "peer_id": peer, "peer": account_name(peer), "peer_kind": "agent" if peer == c["agent"] else "human",
            "human_id": c["human"], "agent_id": c["agent"], "read_upto": cursor, "unread": unread,
            "last_id": last["id"] if last else 0, "preview": ("Сообщение удалено" if last["deleted_ts"] else last["body"][:100]) if last else "",
            "last_ts": last["ts"] if last else c["created"]}


def recipients_list(s):
    return [x for x in (s or "").split(",") if x]


def project_agents(pid):
    return [r["user"] for r in db().execute("SELECT m.user FROM members m JOIN users u ON u.name = m.user "
                                            "WHERE m.project_id = ? AND m.role = 'agent' AND u.revoked = 0 "
                                            "ORDER BY m.user", (pid,)) if member_role(pid, r["user"])]


def expected_recipients(pid, author, to):
    """Who should get a message: the addressees, or every agent of the project except the author."""
    if to:
        return [x for x in to if x != author]
    return [a for a in project_agents(pid) if a != author]


def msg_dicts(rows, me=None, full=True):
    purge_expired_secrets()
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    qs = ",".join("?" * len(ids))
    con = db()
    atts, receipts, approvals, reactions = {}, {}, {}, {}
    for reaction in con.execute(f"SELECT * FROM reactions WHERE message_id IN ({qs}) ORDER BY ts, user", ids):
        groups = reactions.setdefault(reaction["message_id"], {})
        groups.setdefault(reaction["emoji"], []).append(reaction["user"])
    for a in con.execute(f"SELECT id, message_id, name, size, md5, link, (content IS NOT NULL OR upload_id IS NOT NULL) AS inl "
                         f"FROM attachments WHERE message_id IN ({qs}) ORDER BY id", ids):
        atts.setdefault(a["message_id"], []).append(
            {"id": a["id"], "name": a["name"], "size": a["size"], "md5": a["md5"], "link": a["link"],
             "inline": bool(a["inl"])})
    for r in con.execute(f"SELECT * FROM receipts WHERE message_id IN ({qs})", ids):
        receipts.setdefault(r["message_id"], []).append(r)
    agents = {pid: project_agents(pid) for pid in {r["project_id"] for r in rows}}
    for a in con.execute(f"SELECT a.*, u.owner FROM approvals a LEFT JOIN users u ON u.name = a.user "
                         f"WHERE message_id IN ({qs}) ORDER BY ts", ids):
        approvals.setdefault(a["message_id"], []).append(
            {"by": account_name(a["user"]), "by_id": a["user"], "decision": a["decision"], "note": a["note"], "time": iso(a["ts"])})
    thread_ids = list({r["thread_id"] for r in rows if r["thread_id"]})
    project_ids = list({r["project_id"] for r in rows})
    visible, visible_args = message_visibility(me)
    thread_counts = {r[0]: r[1] for r in con.execute(
        "SELECT m.thread_id,COUNT(*) FROM messages m WHERE m.thread_id IN (" +
        ",".join("?" for _ in thread_ids) + ") AND m.project_id IN (" + ",".join("?" for _ in project_ids) +
        ") AND m.deleted_ts IS NULL AND m.reply_to IS NOT NULL AND m.type NOT IN ('status','approval') AND " +
        visible + " GROUP BY m.thread_id", thread_ids + project_ids + visible_args)} if thread_ids else {}
    parent_ids = list({r["reply_to"] for r in rows if r["reply_to"]})
    parents = {r["id"]: r for r in con.execute(
        "SELECT m.id,m.author,m.project_id,m.conversation_id,m.deleted_ts,substr(m.body,1,400) AS body "
        "FROM messages m WHERE m.id IN (" + ",".join("?" for _ in parent_ids) +
        ") AND m.project_id IN (" + ",".join("?" for _ in project_ids) + ") AND " + visible,
        parent_ids + project_ids + visible_args)} if parent_ids else {}
    out = []
    for r in rows:
        rc = receipts.get(r["id"], [])
        mine = next((x for x in rc if x["user"] == me), None)
        d = {
            "id": r["id"], "time": iso(r["ts"]), "ts": r["ts"],
            "author": account_name(r["author"]), "author_id": r["author"], "author_kind": r["author_kind"],
            "owner": account_name(r["owner"]), "owner_id": r["owner"], "host": r["host"],
            "to": [account_name(x) for x in recipients_list(r["recipients"])],
            "to_ids": recipients_list(r["recipients"]), "topic": r["topic"], "type": r["type"],
            "priority": r["priority"], "subject": r["subject"],
            "body": r["body"] if full else r["body"][:400],
            "reply_to": r["reply_to"], "thread_id": r["thread_id"],
            "reply_preview": None,
            "reply_count": thread_counts.get(r["thread_id"], 0),
            "conversation_id": r["conversation_id"],
            "revision": r["revision"], "edited": iso(r["edited_ts"]), "deleted": bool(r["deleted_ts"]),
            "can_edit": me==r["author"] and r["author_kind"]=="human" and r["type"] in USER_TYPES and not r["deleted_ts"],
            "can_delete": me==r["author"] and r["author_kind"]=="human" and r["type"] in USER_TYPES and not r["deleted_ts"],
            "needs_approval": bool(r["needs_approval"]), "approver": account_name(r["approver"]), "approver_id": r["approver"],
            "approvals": approvals.get(r["id"], []),
            "reactions": [{"emoji": emoji, "count": len(names), "mine": me in names, "users": [account_name(x) for x in names]}
                          for emoji in REACTION_EMOJI
                          for names in [reactions.get(r["id"], {}).get(emoji, [])] if names],
            "status": None if r["status"] is None else {
                "state": r["status"], "by": account_name(r["status_by"]), "by_id": r["status_by"], "time": iso(r["status_ts"]),
                "ref": r["status_ref"], "note": r["status_note"]},
            "result": json.loads(r["result"]) if r["result"] else None,
            "attachments": atts.get(r["id"], []),
            "read": bool(mine and mine["read_ts"]), "acked": bool(mine and mine["ack_ts"]),
            "acked_by": [account_name(x["user"]) for x in rc if x["ack_ts"]],
        }
        parent = parents.get(r["reply_to"])
        if parent and parent["project_id"] == r["project_id"] and parent["conversation_id"] == r["conversation_id"]:
            d["reply_preview"] = {"id": parent["id"], "author": account_name(parent["author"]),
                                  "author_id": parent["author"], "deleted": bool(parent["deleted_ts"]),
                                  "body": "" if parent["deleted_ts"] else parent["body"][:400]}
        # per-recipient delivery: delivered (their watch/inbox got it) -> read -> acked
        by_user = {x["user"]: x for x in rc if x["user"] != r["author"]}
        to = d["to_ids"] or [a for a in agents[r["project_id"]] if a != r["author"]]
        d["receipts"] = [{"user": account_name(u), "user_id": u,
                          "delivered": iso(by_user[u]["delivered_ts"] or by_user[u]["read_ts"]) if u in by_user else None,
                          "read": iso(by_user[u]["read_ts"]) if u in by_user else None,
                          "acked": iso(by_user[u]["ack_ts"]) if u in by_user else None}
                         for u in to + [u for u in by_user if u not in to]]
        secret = con.execute("SELECT recipient,expires,claimed_ts,consumed,revoked FROM secret_envelopes WHERE message_id=?", (r["id"],)).fetchone()
        if secret:
            d["can_edit"] = False
            d["secret"] = dict(secret)
            d["secret"]["state"] = "revoked" if secret["revoked"] else "received" if secret["consumed"] else "expired" if secret["expires"] <= time.time() else "pending"
        if r["deleted_ts"]:
            d.update(body="Сообщение удалено",subject="Сообщение удалено",topic="",attachments=[],reactions=[],approvals=[],
                     result=None,status=None,needs_approval=False,priority="normal")
            d.pop("secret",None)
        out.append(d)
    return out


def insert_message(user, host, data, pid, system=False, secret=None):
    # Keep file adoption, cancellation and disk-space checks atomic across senders.
    with UPLOAD_LOCK, DB_LOCK:
        return _insert_message(user, host, data, pid, system, secret)


def _insert_message(user, host, data, pid, system=False, secret=None):
    """Validate and store a message in project pid. Returns (id, duplicate)."""
    con = db()
    cid = data.get("conversation_id") or None
    if data.get("reply_to") is not None:
        parent_scope = get_message_row(int(data["reply_to"]), user)
        if parent_scope["project_id"] != pid or ("conversation_id" in data and cid != parent_scope["conversation_id"]):
            raise ApiError(409, _ui_text('ответ должен оставаться в исходном чате'))
        cid = parent_scope["conversation_id"]
    conversation = conversation_row(cid, user, pid) if cid else None
    idem = str(data.get("idem_key") or "")[:100] or None
    if idem:
        dup = con.execute("SELECT id,project_id,conversation_id FROM messages WHERE author=? AND idem_key=?", (user["name"], idem)).fetchone()
        if dup:
            if dup["project_id"] != pid or dup["conversation_id"] != cid:
                raise ApiError(409, _ui_text('ключ сообщения уже использован в другом чате'))
            return dup["id"], True
    mtype = data.get("type") or "info"
    allowed = USER_TYPES + (SYSTEM_TYPES if system else ())
    if mtype not in allowed:
        raise ApiError(400, f"{_ui_text('type должен быть одним из: ')}{', '.join(USER_TYPES)}")
    priority = data.get("priority") or "normal"
    if priority not in PRIORITIES:
        raise ApiError(400, "priority: normal | urgent")
    body = str(data.get("body") or "")
    subject = str(data.get("subject") or "").strip()
    if not system:
        # a message is one text; an explicit subject (older clients, tg.py -s) becomes its first line
        if subject and not body.lstrip().startswith(subject):
            body = subject + ("\n" + body if body.strip() else "")
        subject = ""
    if len(body) > MAX_BODY_CHARS:
        raise ApiError(413, f"{_ui_text('текст длиннее ')}{MAX_BODY_CHARS}{_ui_text(' символов — положи его во вложение')}")
    if not subject:
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        subject = first[:120]   # only a short preview now: search, status lines, the journal
    if not subject and not data.get("attachments"):
        raise ApiError(400, _ui_text('пустое сообщение'))
    subject = subject.replace("\n", " ")[:200]
    topic = str(data.get("topic") or "").strip()[:60]

    using_ids = system or "to_ids" in data
    to = data.get("to_ids") if "to_ids" in data else data.get("to")
    to = to or []
    if isinstance(to, str):
        to = [x.strip() for x in to.split(",")]
    if not isinstance(to, list) or any(not isinstance(x, str) for x in to):
        raise ApiError(400, _ui_text('адресаты: список имён или to_ids'))
    to = [x for x in to if x and (x != "all" or using_ids)]
    if to:
        resolved = []
        for value in to:
            matches = find_accounts(value, pid, account_id=value if using_ids else None)
            if len(matches) != 1 or matches[0]["revoked"]:
                raise ApiError(400, _ui_text('нет таких адресатов в выбранном проекте'))
            if matches[0]["name"] not in resolved:
                resolved.append(matches[0]["name"])
        to = resolved

    reply_to = data.get("reply_to")
    thread_id = None
    if reply_to is not None:
        parent = con.execute("SELECT * FROM messages WHERE id = ? AND project_id = ?", (int(reply_to), pid)).fetchone()
        if not parent:
            raise ApiError(404, f"{_ui_text('сообщения #')}{reply_to}{_ui_text(' нет')}")
        if not system and not data.get("to_ids") and any(
                x != parent["author"] and account_name(x).lower() == account_name(parent["author"]).lower() for x in to):
            raise ApiError(409, _ui_text('за этим именем теперь другой аккаунт: выбери адресата по to_ids'))
        reply_to = parent["id"]
        thread_id = parent["thread_id"]
        if not topic:
            topic = parent["topic"]

    result = data.get("result") or None
    if result is not None:
        if not isinstance(result, dict):
            raise ApiError(400, _ui_text('result должен быть объектом'))
        result = {k: v for k, v in result.items() if k in RESULT_FIELDS and v not in (None, "")}
        for k, v in result.items():
            if not isinstance(v, (str, int, float)) or len(str(v)) > 500:
                raise ApiError(400, f"result.{k}{_ui_text(': строка или число до 500 символов')}")
        result = result or None

    needs_approval = bool(data.get("needs_approval")) or mtype == "decision"
    approver = str(data.get("approver") or "").strip() or None
    if approver:
        needs_approval = True
        matches = find_accounts(approver, pid, kind="human")
        approver = matches[0]["name"] if len(matches) == 1 else None
        row = con.execute("SELECT u.kind FROM members m JOIN users u ON u.name = m.user "
                          "WHERE m.project_id = ? AND m.user = ? AND u.revoked = 0", (pid, approver)).fetchone()
        if not row or row["kind"] != "human":
            raise ApiError(400, _ui_text('одобряющий должен быть человеком из этого проекта'))
        if to and approver not in to and approver != user["name"]:
            to.append(approver)   # the request must reach the person who decides
    if conversation:
        parties = (conversation["human"], conversation["agent"])
        if any(key not in parties for key in to) or (approver and approver not in parties):
            raise ApiError(400, _ui_text('в личном чате только два участника'))
        if data.get("claim"):
            raise ApiError(400, _ui_text('заявки проекта публикуются в общем чате'))
        to = [key for key in parties if key != user["name"]]
    status = "open" if mtype in TRACKED_TYPES else None

    # attachments: decode + validate before writing anything
    atts = []
    attachments = data.get("attachments") or []
    if not isinstance(attachments, list) or len(attachments) > 30 or any(not isinstance(a, dict) for a in attachments):
        raise ApiError(400, _ui_text('attachments: список, не больше 30 вложений'))
    for a in attachments:
        name = attachment_name(a.get("name"))
        check_secret("имя вложения", name)
        if a.get("upload_id"):
            upload = own_upload(user, pid, a["upload_id"])
            if upload["attachment_id"] or upload["md5"] is None:
                raise ApiError(409, _ui_text('файл ещё не загружен полностью или уже отправлен'))
            atts.append((upload["name"], upload["size"], upload["md5"], None, None, upload["id"]))
        elif a.get("content_b64") is not None:
            try:
                raw = base64.b64decode(a["content_b64"], validate=True)
            except Exception:
                raise ApiError(400, f"{_ui_text('вложение ')}{name}{_ui_text(': битый base64')}")
            if len(raw) > Cfg.max_inline:
                raise ApiError(413, f"{_ui_text('вложение ')}{name}{_ui_text(' больше ')}{Cfg.max_inline}{_ui_text(' байт — пришли ссылкой и md5')}")
            check_secret(f"вложение {name}", raw.decode("utf-8", errors="replace"))
            atts.append((name, len(raw), hashlib.md5(raw).hexdigest(), None, raw, None))
        elif a.get("link"):
            link = str(a["link"])[:1000]
            check_secret(f"ссылка {name}", link)
            atts.append((name, a.get("size"), (a.get("md5") or None), link, None, None))
        else:
            raise ApiError(400, f"{_ui_text('вложение ')}{name}{_ui_text(': нужен content_b64 или link')}")
    if len(atts) > 30:
        raise ApiError(400, _ui_text('не больше 30 вложений'))
    upload_ids = [a[5] for a in atts if a[5]]
    if len(set(upload_ids)) != len(upload_ids):
        raise ApiError(400, _ui_text('один файл нельзя прикрепить дважды'))
    if any(a[4] is not None or a[5] for a in atts):
        require_file_space(sum(len(a[4]) * 3 for a in atts if a[4] is not None))

    check_secret("тема", subject)
    if user["kind"] == "agent" and not system and data.get("allow_lossy_text") is not True:
        problem=text_encoding_problem(body)
        if problem:
            raise ApiError(422,"Сообщение НЕ отправлено: "+problem+". Отправь исходный текст из файла в правильной кодировке. "
                           "Для намеренных знаков ?/� — allow_lossy_text=true; секрет-фильтр остаётся включён.")
    check_secret("текст", body)
    check_secret("топик", topic)
    if result:
        check_secret("результат", json.dumps(result, ensure_ascii=False))

    idem = data.get("idem_key")
    idem = str(idem)[:100] if idem else None
    with DB_LOCK:
        if idem:
            dup = con.execute("SELECT id FROM messages WHERE author = ? AND idem_key = ?",
                              (user["name"], idem)).fetchone()
            if dup:
                return dup["id"], True
        con.execute("BEGIN IMMEDIATE")
        try:
            cur = con.execute(
                "INSERT INTO messages(ts, author, author_kind, owner, host, recipients, topic, type, priority,"
                " subject, body, reply_to, thread_id, needs_approval, status, result, idem_key, project_id, approver,conversation_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), user["name"], user["kind"], user["owner"], (host or "")[:120],
                 ("," + ",".join(to) + ",") if to else "", topic, mtype, priority, subject, body,
                 reply_to, thread_id, int(needs_approval), status,
                 json.dumps(result, ensure_ascii=False) if result else None, idem, pid, approver, cid))
            mid = cur.lastrowid
            if secret:
                con.execute("INSERT INTO secret_envelopes(message_id,recipient,key_id,envelope,expires) VALUES (?,?,?,?,?)",
                            (mid, conversation["agent"], secret["key_id"], json.dumps(secret["envelope"]), secret["expires"]))
            if thread_id is None:
                con.execute("UPDATE messages SET thread_id = ? WHERE id = ?", (mid, mid))
            for name, size, md5, link, raw, upload_id in atts:
                aid = con.execute("INSERT INTO attachments(message_id,name,size,md5,link,content,upload_id) VALUES (?,?,?,?,?,?,?)",
                                  (mid, name, size, md5, link, raw, upload_id)).lastrowid
                if upload_id:
                    con.execute("UPDATE file_uploads SET attachment_id=? WHERE id=?", (aid, upload_id))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    with NEW_MSG:
        LAST_ID[0] = max(LAST_ID[0], mid)
        NEW_MSG.notify_all()
    if Cfg.telegram and not cid:
        TG_QUEUE.put(mid)
    return mid, False


def get_message_row(mid, user=None):
    """A message by id; with user, only if the user is in its project (otherwise it "does not exist")."""
    r = db().execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    if not r or (user is not None and not can_read_message(r, user)):
        raise ApiError(404, f"{_ui_text('сообщения #')}{mid}{_ui_text(' нет')}")
    return r


# Placeholders: me (author), "%,me,%" (explicit recipient only: broadcasts do not nag), me (reply author).
# An ack is a receipt, not a message, so it never answers a question; a later message of mine in the thread does.
WAITING_SQL = ("(m.deleted_ts IS NULL AND m.author != ? AND m.recipients LIKE ? AND ((m.type = 'request' AND m.status = 'open') OR"
               " (m.type = 'question' AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.thread_id = m.thread_id"
               " AND r.id > m.id AND r.author = ? AND r.deleted_ts IS NULL AND r.type NOT IN ('status','approval')))))"
               " AND NOT EXISTS (SELECT 1 FROM waiting_dismissals d WHERE d.message_id=m.id AND d.user=? AND d.revision=m.revision)")


def waiting_summary(me, pid):
    """Counts and ages of what waits for me and of requests I took and did not close (for the hook and inbox)."""
    con, now = db(), time.time()
    visible, va = message_visibility(me)
    rows = con.execute(f"SELECT m.id, m.type, m.author, m.ts FROM messages m WHERE m.project_id = ? AND {WAITING_SQL} AND {visible}"
                       " ORDER BY m.id", [pid, me, f"%,{me},%", me, me] + va).fetchall()
    taken = con.execute(f"SELECT m.id, m.ts FROM messages m WHERE deleted_ts IS NULL AND project_id = ? AND type = 'request' AND status = 'taken'"
                        f" AND status_by = ? AND {visible} ORDER BY id", [pid, me] + va).fetchall()
    return {"waiting_me": {"count": len(rows), "oldest_age": round(now - rows[0]["ts"]) if rows else None,
                           "items": [{"id": r["id"], "type": r["type"], "author": account_name(r["author"])} for r in rows[:10]]},
            "taken_open": {"count": len(taken), "oldest_age": round(now - taken[0]["ts"]) if taken else None,
                           "ids": [r["id"] for r in taken[:10]]}}


def list_messages(me, q, pid):
    if not member_role(pid,me["name"]):
        raise ApiError(404, _ui_text('нет доступа к проекту'))
    where, args = ["m.project_id = ?"], [pid]
    visible, va = message_visibility(me["name"])
    where.append(visible); args += va
    if q.get("for_me")=="1" or any(q.get(key) for key in ("q","results","open","waiting_me","pending_approval","unacked","unread")):
        where.append("m.deleted_ts IS NULL")
    cid = q.get("conversation")
    if cid and cid != "all":
        conversation_row(cid, me, pid)
        where.append("m.conversation_id=?"); args.append(cid)
    elif cid != "all" and q.get("for_me") != "1":
        where.append("m.conversation_id IS NULL")
    con = db()
    after = int(q.get("after") or 0)
    if after:
        where.append("m.id > ?")
        args.append(after)
    if q.get("before"):
        where.append("m.id < ?")
        args.append(int(q["before"]))
    if q.get("topic"):
        where.append("m.topic = ?")
        args.append(q["topic"])
    if q.get("type"):
        types = [t for t in q["type"].split(",") if t]
        where.append(f"m.type IN ({','.join('?' * len(types))})")
        args += types
    if q.get("author_id"):
        where.append("m.author = ?"); args.append(q["author_id"])
    elif q.get("author"):
        where.append("m.author IN (SELECT name FROM users WHERE lower(COALESCE(display_name,name))=lower(?))")
        args.append(q["author"])
    if q.get("thread"):
        where.append("m.thread_id = ?")
        args.append(int(q["thread"]))
    if q.get("q"):
        pat = "%" + q["q"].lower() + "%"
        where.append("(pylower(m.subject) LIKE ? OR pylower(m.body) LIKE ? OR pylower(m.topic) LIKE ?"
                     " OR pylower(COALESCE(m.result,'')) LIKE ? OR pylower(COALESCE(m.status_ref,'')) LIKE ?)")
        args += [pat] * 5
    if q.get("for_me") == "1":
        where.append("m.author != ? AND (m.recipients = '' OR m.recipients LIKE ?)")
        args += [me["name"], f"%,{me['name']},%"]
    if q.get("waiting_me") == "1":
        where.append(WAITING_SQL)
        args += [me["name"], f"%,{me['name']},%", me["name"], me["name"]]
    if q.get("unacked") == "1":
        where.append("NOT EXISTS (SELECT 1 FROM receipts r WHERE r.user = ? AND r.message_id = m.id"
                     " AND r.ack_ts IS NOT NULL)")
        args.append(me["name"])
    if q.get("unread") == "1":
        where.append("NOT EXISTS (SELECT 1 FROM receipts r WHERE r.user = ? AND r.message_id = m.id"
                     " AND r.read_ts IS NOT NULL)")
        args.append(me["name"])
    if q.get("open") == "1":
        where.append("m.status IN ('open','taken')")
    if q.get("pending_approval") in ("1", "mine"):
        where.append("m.needs_approval = 1 AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.message_id = m.id)")
        if q["pending_approval"] == "mine":
            where.append("(m.approver = ? OR m.approver IS NULL)")
            args.append(me["name"])
    if q.get("results") == "1":
        where.append("m.result IS NOT NULL")
    limit = max(1, min(int(q.get("limit") or 50), 1000))
    sql = "SELECT m.* FROM messages m" + (" WHERE " + " AND ".join(where) if where else "")
    if after:
        rows = con.execute(sql + " ORDER BY m.id ASC LIMIT ?", args + [limit]).fetchall()
    else:
        rows = con.execute(sql + " ORDER BY m.id DESC LIMIT ?", args + [limit]).fetchall()[::-1]
    return msg_dicts(rows, me["name"], full=q.get("full", "1") == "1")


def mark(me, ids, kind):
    now = time.time()
    con = db()
    with DB_LOCK:
        for mid in ids:
            message = con.execute("SELECT project_id FROM messages WHERE id=?", (mid,)).fetchone()
            if not message:
                continue
            con.execute("INSERT OR IGNORE INTO receipts(user, message_id) VALUES (?, ?)", (me["name"], mid))
            changed = con.execute("UPDATE receipts SET delivered_ts = ? WHERE user = ? AND message_id = ? AND delivered_ts IS NULL",
                                  (now, me["name"], mid)).rowcount
            if kind in ("read", "ack"):
                changed += con.execute("UPDATE receipts SET read_ts = ? WHERE user = ? AND message_id = ? AND read_ts IS NULL",
                                       (now, me["name"], mid)).rowcount
            if kind == "ack":
                changed += con.execute("UPDATE receipts SET ack_ts = ? WHERE user = ? AND message_id = ? AND ack_ts IS NULL",
                                       (now, me["name"], mid)).rowcount
            if changed:
                with NEW_MSG:
                    pid = message["project_id"]
                    RECEIPT_REVISIONS[pid] = RECEIPT_REVISIONS.get(pid, 0) + 1
                    NEW_MSG.notify_all()


def set_status(me, host, mid, data):
    state = data.get("state")
    if state not in STATES:
        raise ApiError(400, f"state: {' | '.join(STATES)}")
    ref = str(data.get("ref") or "")[:1000]
    note = str(data.get("note") or "")[:5000]
    check_secret("ref", ref)
    check_secret("note", note)
    m = get_message_row(mid, me)
    require_active_message(m)
    if m["type"] not in TRACKED_TYPES:
        raise ApiError(400, f"{_ui_text('статус есть только у ')}{', '.join(TRACKED_TYPES)}; #{mid} — {m['type']}")
    with DB_LOCK:
        m=get_message_row(mid,me);require_active_message(m)
        db().execute("UPDATE messages SET status = ?, status_by = ?, status_ts = ?, status_ref = ?, status_note = ?"
                     " WHERE id = ?", (state, me["name"], time.time(), ref or None, note or None, mid))
    words = {"open": "снова открыто", "taken": "взято", "done": "сделано", "rejected": "отклонено"}
    body = "\n".join(x for x in (f"ref: {ref}" if ref else "", note) if x)
    return insert_message(me, host, {
        "type": "status", "reply_to": mid, "subject": f"#{mid} {words[state]}: {m['subject'][:80]}",
        "body": body, "to": [m["author"]] if m["author"] != me["name"] else [],
        "priority": "normal"}, m["project_id"], system=True)


def set_approval(me, host, mid, data):
    if me["kind"] != "human":
        raise ApiError(403, _ui_text('одобрять может только человек'))
    decision = data.get("decision")
    if decision not in ("approve", "deny"):
        raise ApiError(400, "decision: approve | deny")
    note = str(data.get("note") or "")[:5000]
    check_secret("note", note)
    m = get_message_row(mid, me)
    require_active_message(m)
    if not m["needs_approval"]:
        raise ApiError(400, f"#{mid}{_ui_text(' не просит одобрения')}")
    if m["approver"] and m["approver"] != me["name"]:
        raise ApiError(403, f"{_ui_text('решение по #')}{mid}{_ui_text(' принимает только ')}{account_name(m['approver'])}")
    now = time.time()
    with DB_LOCK:
        m=get_message_row(mid,me);require_active_message(m)
        revision=data.get("revision",0)
        if type(revision) is not int or revision!=m["revision"]:
            raise ApiError(409,_ui_text('текст сообщения изменился; прочитай актуальную версию перед решением'))
        db().execute("INSERT OR REPLACE INTO approvals(message_id, user, decision, note, ts) VALUES (?,?,?,?,?)",
                     (mid, me["name"], decision, note or None, now))
        if m["type"] == "decision":
            db().execute("UPDATE messages SET status = ?, status_by = ?, status_ts = ? WHERE id = ?",
                         ("done" if decision == "approve" else "rejected", me["name"], now, mid))
    word = "ОДОБРЕНО" if decision == "approve" else "ОТКАЗАНО"
    return insert_message(me, host, {
        "type": "approval", "reply_to": mid, "subject": f"#{mid} {word} ({account_name(me['name'])}): {m['subject'][:80]}",
        "body": note, "to": [], "priority": "urgent" if m["priority"] == "urgent" else "normal"}, m["project_id"], system=True)


# ---------------------------------------------------------------- experiment claims
def norm_key(s):
    return " ".join(str(s or "").lower().split())


CLAIM_HOURS = 24      # default lifetime of a claim: a forgotten one must not block others forever


def claim_dict(r):
    exp = r["expires"]        # NULL = old claim, never expires
    left = round(exp - time.time()) if exp is not None and r["released"] is None else None
    return {"expires": iso(exp), "expires_in": left, "expired": left is not None and left < 0,
            "id": r["id"], "key": r["display_key"], "norm_key": r["key"], "topic": r["topic"],
            "owner": account_name(r["owner"]), "owner_id": r["owner"],
            "owner_kind": r["owner_kind"], "host": r["host"], "note": r["note"], "until": r["until"],
            "created": iso(r["created"]), "released": iso(r["released"]), "active": r["released"] is None,
            "release_note": r["release_note"], "release_msg": r["release_msg"]}


def release_claim(row, note=None, msg=None):
    db().execute("UPDATE claims SET released = ?, release_note = ?, release_msg = ? WHERE id = ?",
                 (time.time(), note or None, msg, row["id"]))
    return claim_dict(db().execute("SELECT * FROM claims WHERE id = ?", (row["id"],)).fetchone())


def list_claims(q, pid):
    where, args = ["project_id = ?"], [pid]
    if q.get("active") in ("0", "1"):
        where.append("released IS NULL" if q["active"] == "1" else "released IS NOT NULL")
    if q.get("topic"):
        where.append("topic = ?")
        args.append(q["topic"])
    if q.get("owner_id"):
        where.append("owner = ?"); args.append(q["owner_id"])
    elif q.get("owner"):
        where.append("owner IN (SELECT name FROM users WHERE lower(COALESCE(display_name,name))=lower(?))")
        args.append(q["owner"])
    limit = max(1, min(int(q.get("limit") or 200), 1000))
    sql = ("SELECT * FROM claims" + (" WHERE " + " AND ".join(where) if where else "") +
           " ORDER BY (released IS NOT NULL), COALESCE(released, created) DESC, id DESC LIMIT ?")
    return [claim_dict(r) for r in db().execute(sql, args + [limit])]


def claim_text(data, field, limit):
    v = str(data.get(field) or "").strip()[:limit]
    check_secret(field, v)
    return v


# ---------------------------------------------------------------- telegram mirror
def telegram_worker():
    api = f"https://api.telegram.org/bot{Cfg.telegram['bot_token']}/sendMessage"
    while True:
        mid = TG_QUEUE.get()
        try:
            with DB_LOCK:
                d = msg_dicts([get_message_row(mid)])[0]
            head = f"#{d['id']} {d['type']}" + (" СРОЧНО" if d["priority"] == "urgent" else "")
            head += f" — {d['author']} ({'человек' if d['author_kind'] == 'human' else 'агент'}@{d['host']})"
            if d["to"]:
                head += " → " + ", ".join(d["to"])
            if d["topic"]:
                head += f" [{d['topic']}]"
            if d["needs_approval"]:
                head += " ⚠ нужно одобрение"
            text = f"{head}\n{d['subject']}"
            if d["body"] and d["body"].strip() != d["subject"]:
                text += "\n\n" + d["body"][:3000]
            if d["attachments"]:
                text += "\n📎 " + ", ".join(a["name"] for a in d["attachments"])
            payload = urllib.parse.urlencode({"chat_id": Cfg.telegram["chat_id"], "text": text[:4000],
                                              "disable_web_page_preview": "true"}).encode()
            urllib.request.urlopen(api, data=payload, timeout=20).read()
        except Exception as e:  # mirror is best effort
            print(f"telegram mirror: #{mid}: {type(e).__name__}", file=sys.stderr)


# ---------------------------------------------------------------- HTTP
ROUTES = []


def route(method, pattern, auth=True, raw=False):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, auth, fn, raw))
        return fn
    return deco


class Handler(BaseHTTPRequestHandler):
    server_version = "telegent/" + VERSION

    def log_message(self, fmt, *args):
        path = self.path.split("?")[0]
        sys.stderr.write(f"{datetime.now(Cfg.tz).strftime('%m-%d %H:%M:%S')} {self.command} {path} {args[1] if len(args) > 1 else ''}\n")

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        _UI_CONTEXT.language = "en" if self.headers.get("X-Telegent-Language", "ru") == "en" else "ru"
        parsed = urllib.parse.urlsplit(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            for m, rx, auth, fn, raw in ROUTES:
                mt = rx.match(parsed.path)
                if m == method and mt:
                    user = self.auth() if auth else None
                    data = self.read_json() if method == "POST" and not raw else None
                    with_args = [int(x) if x.isdigit() else x for x in mt.groups()]
                    res = fn(self, user, q, data, *with_args)
                    if res is not None:
                        self.send_json(200, res)
                    return
            raise ApiError(404, _ui_text('нет такого пути'))
        except ApiError as e:
            self.send_json(e.code, dict(e.extra, error=_UI_EN.get(e.msg, e.msg) if _UI_CONTEXT.language == "en" else e.msg))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            if (isinstance(e, OSError) and e.errno == 28) or (isinstance(e, sqlite3.Error) and "disk is full" in str(e).lower()):
                self.send_json(507, {"error": "На сервере закончилось место. Можно отправить текст без вложения."})
                return
            import traceback
            traceback.print_exc()
            self.send_json(500, {"error": f"server error: {type(e).__name__}: {e}"})

    def auth(self):
        h = self.headers.get("Authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        if not tok:
            raise ApiError(401, _ui_text('нужен токен (Authorization: Bearer ...)'))
        th = token_hash(tok)
        self.session_hash = None
        with DB_LOCK:
            u = db().execute("SELECT * FROM users WHERE token_hash = ? AND revoked = 0", (th,)).fetchone()
            if not u and tok.startswith("tgs_"):
                u = db().execute("SELECT u.* FROM sessions s JOIN users u ON u.name = s.user "
                                 "WHERE s.token_hash = ? AND u.revoked = 0", (th,)).fetchone()
                if u:
                    self.session_hash = th
                    db().execute("UPDATE sessions SET last_ts = ? WHERE token_hash = ?", (time.time(), th))
        if not u:
            raise ApiError(401, _ui_text('неизвестный или отозванный токен (или сессия закрыта)'))
        touch(u)
        return u

    def client_ip(self):
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def host(self):
        return self.headers.get("X-Telegent-Host", "")[:120] or "web"

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, _ui_text('некорректный Content-Length'))
        if n < 0 or n > MAX_REQUEST or self.headers.get("Transfer-Encoding"):
            raise ApiError(413, _ui_text('запрос слишком большой'))
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("object required")
            return data
        except Exception:
            raise ApiError(400, _ui_text('тело запроса — не JSON'))

    def send_json(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_bytes(code, body, "application/json; charset=utf-8", extra)

    def send_bytes(self, code, body, ctype, extra=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if "Cache-Control" not in (extra or {}):
                self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


@route("GET", "/", auth=False)
def page_index(h, user, q, data):
    with open(os.path.join(HERE, "web.html"), "rb") as f:
        h.send_bytes(200, f.read(), "text/html; charset=utf-8",
                     {"Content-Security-Policy": "default-src 'self' 'unsafe-inline'; img-src 'self' data: blob:",
                      "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})


# Android Chrome shows page notifications only through a service worker; this one just handles clicks
SW_JS = b"""self.addEventListener("notificationclick", e => {
  e.notification.close();
  const id = e.notification.data && e.notification.data.id;
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    const w = list[0];
    if (!w) return self.clients.openWindow("/");
    if (id) w.postMessage({telegentJump: id});
    return w.focus();
  }));
});
"""


@route("GET", "/sw.js", auth=False)
def page_sw(h, user, q, data):
    h.send_bytes(200, SW_JS, "text/javascript; charset=utf-8",
                 {"Service-Worker-Allowed": "/", "Cache-Control": "max-age=300"})


@route("GET", "/api/info", auth=False)
def api_info(h, user, q, data):
    return {"version": VERSION, "server_time": iso(time.time()), "types": USER_TYPES, "states": STATES,
            "result_fields": RESULT_FIELDS, "max_inline_bytes": Cfg.max_inline, "launcher_url": Cfg.launcher_url}


def can_rename_project(p, user):
    return not p["creator_unknown"] and user["kind"] == "human" and p["created_by"] == user["name"]


def project_dict(p, user):
    member = db().execute("SELECT read_upto FROM members WHERE project_id=? AND user=?", (p["id"], user["name"])).fetchone()
    return {"id": p["id"], "slug": p["slug"], "title": p["title"], "role": p["role"],
            "read_upto": member["read_upto"] if member else 0,
            "created_by": None if p["creator_unknown"] else account_name(p["created_by"]),
            "created_by_id": None if p["creator_unknown"] else p["created_by"], "can_rename": can_rename_project(p, user),
            "kind": "development" if p["admin_owner"] else "team",
            "developer_connected": bool(p["service_agent"] and member_role(p["id"], p["service_agent"]))}


def development_project(con, user):
    current = con.execute("SELECT * FROM users WHERE name=?", (user["name"],)).fetchone()
    if not current or current["kind"] != "human" or not current["is_admin"] or current["revoked"]:
        raise ApiError(403, _ui_text('чат с разработчиком доступен только администратору сервера'))
    p = con.execute("SELECT * FROM projects WHERE admin_owner=? AND archived=0", (user["name"],)).fetchone()
    if not p:
        pid = create_project_row(con, "telegent-development", "Разработка telegent", user["name"])
        con.execute("UPDATE projects SET admin_owner=? WHERE id=?", (user["name"], pid))
        add_member(con, pid, user["name"], "owner", user["name"])
    else:
        pid = p["id"]
        if not member_role(pid, user["name"]):
            add_member(con, pid, user["name"], "owner", user["name"])
    return con.execute("SELECT p.*,m.role FROM projects p JOIN members m ON m.project_id=p.id "
                       "WHERE p.id=? AND m.user=?", (pid, user["name"])).fetchone()


@route("POST", "/api/admin/development-chat")
def api_development_chat(h, user, q, data):
    with DB_LOCK:
        con = db()
        con.execute("BEGIN IMMEDIATE")
        try:
            result = project_dict(development_project(con, user), user)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return {"project": result}


def project_title(data):
    title = data.get("title")
    if not isinstance(title, str):
        raise ApiError(400, _ui_text('название проекта должно быть текстом'))
    title = title.strip()
    if not title:
        raise ApiError(400, _ui_text('нужно название проекта'))
    if len(title) > 80:
        raise ApiError(400, _ui_text('название проекта: не больше 80 символов'))
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF
           or c in "\u2028\u2029" for c in title):
        raise ApiError(400, _ui_text('название проекта должно быть одной строкой без управляющих символов'))
    check_secret("название", title)
    return title


@route("GET", "/api/me")
def api_me(h, user, q, data):
    """Who I am, my projects, and (if the request names one, or I am in exactly one) that project's members."""
    with DB_LOCK:
        projects = [project_dict(p, user) for p in my_projects(user["name"])]
        try:
            p = project_for(h, user, q)
        except ApiError:
            p = None
        users = users_with_presence(p["id"]) if p else []
        mine = presence(user)
    return {**account_dict(user),
            "project": project_dict(p, user) if p else None, "projects": projects, "users": users,
            "listening": mine["listening"], "working": mine["working"], "last_listen": mine["last_listen"],
            "has_password": bool(user["pw_hash"]), "session": bool(h.session_hash), "is_admin": bool(user["is_admin"]),
            "server_time": iso(time.time()), "last_id": LAST_ID[0]}


@route("POST", "/api/login", auth=False)
def api_login(h, user, q, data):
    name = str(data.get("name") or "").strip()
    pw = str(data.get("password") or "")
    keys = [f"name:{name.lower()}", f"ip:{h.client_ip()}"]
    login_throttle(keys)
    with DB_LOCK:
        rows = find_accounts(name, kind="human")
        u = rows[0] if rows and not rows[0]["revoked"] else None
    if u and not u["pw_hash"]:
        raise ApiError(400, f"{_ui_text('у ')}{name}{_ui_text(' ещё нет пароля: войди один раз по ссылке с токеном и задай его (кнопка «Пароль»)')}")
    if not u or not check_password(pw, u["pw_hash"]):
        login_failed(keys)
        raise ApiError(401, _ui_text('неверное имя или пароль'))
    _login_fails.pop(keys[0], None)
    return {"token": new_session(u["name"], h.headers.get("User-Agent", "")), **account_dict(u)}


@route("POST", "/api/logout")
def api_logout(h, user, q, data):
    if h.session_hash:
        with DB_LOCK:
            db().execute("DELETE FROM sessions WHERE token_hash = ?", (h.session_hash,))
    return {"ok": True, "session_closed": bool(h.session_hash)}


@route("POST", "/api/password")
def api_password(h, user, q, data):
    if user["kind"] != "human":
        raise ApiError(403, _ui_text('пароль бывает только у людей; агенты входят по токену'))
    new = str(data.get("new") or "")
    if len(new) < PW_MIN_LEN:
        raise ApiError(400, f"{_ui_text('пароль короче ')}{PW_MIN_LEN}{_ui_text(' символов')}")
    if user["pw_hash"]:
        keys = [f"name:{account_name(user['name']).lower()}", f"ip:{h.client_ip()}"]
        login_throttle(keys)
        if not check_password(str(data.get("old") or ""), user["pw_hash"]):
            login_failed(keys)
            raise ApiError(403, _ui_text('текущий пароль неверный'))
    with DB_LOCK:
        db().execute("UPDATE users SET pw_hash = ? WHERE name = ?", (hash_password(new), user["name"]))
        # other devices logged in with the old password are signed out; this one stays
        db().execute("DELETE FROM sessions WHERE user = ? AND token_hash != ?", (user["name"], h.session_hash or ""))
    return {"ok": True}


def invite_link(h, code):
    """Link to send: via the permanent entry page if there is one, else straight to this server."""
    if Cfg.launcher_url:
        base = Cfg.launcher_url
    else:
        proto = h.headers.get("X-Forwarded-Proto") or "http"
        base = f"{proto}://{h.headers.get('Host', 'localhost')}/"
    return base + "#invite=" + code


def require_owner(p):
    if p["role"] != "owner":
        raise ApiError(403, f"{_ui_text('это может только владелец проекта «')}{p['slug']}»")


def require_human(user):
    if user["kind"] != "human":
        raise ApiError(403, _ui_text('приглашает и добавляет участников только человек'))


@route("POST", "/api/invites")
def api_invite_create(h, user, q, data):
    """People may invite their own agents; owners may also invite people and others' agents."""
    p = project_for(h, user, q)
    require_human(user)
    name = str(data.get("name") or "").strip()
    with DB_LOCK:
        owner = resolve_account(data.get("owner"), p["id"], kind="human", account_id=data.get("owner_id"))["name"] \
            if data.get("owner") or data.get("owner_id") else user["name"]
        selected = None
        if data.get("account_id"):
            selected = managed_account(h, user, q, data)
        if p["role"] != "owner":
            matches = find_accounts(name, p["id"])
            existing = selected or (matches[0] if matches else None)
            pending = db().execute("SELECT kind,owner FROM invites WHERE lower(name)=lower(?) AND project_id=? "
                                   "AND purpose!='reset' AND used_ts IS NULL AND expires_ts>?",
                                   (name, p["id"], time.time())).fetchall()
            if (data.get("kind") != "agent" or owner != user["name"]
                    or any(r["kind"] != "agent" or r["owner"] != user["name"]
                           for r in ([existing] if existing else []) + pending)):
                raise ApiError(403, _ui_text('участник может пригласить своего агента; людей приглашает владелец проекта'))
        inv = create_invite(user["name"], name, data.get("kind"), owner, p["id"],
                            account_id=selected["name"] if selected else None)
    inv["link"] = invite_link(h, inv["code"])
    inv["project"] = p["slug"]
    return inv


@route("POST", "/api/reset")
def api_reset(h, user, q, data):
    """A new password / token by a one-time link: the server admin for anyone, a person for their own agents."""
    require_human(user)
    with DB_LOCK:
        u = managed_account(h, user, q, data)
        if not (user["is_admin"] or (u["kind"] == "agent" and u["owner"] == user["name"])):
            raise ApiError(403, _ui_text('сбросить можно токен своего агента; пароль человека сбрасывает админ сервера'))
        inv = create_invite(user["name"], account_name(u["name"]), reset=True, account_id=u["name"])
    inv["link"] = invite_link(h, inv["code"])
    return inv


@route("GET", "/api/invites")
def api_invite_list(h, user, q, data):
    p = project_for(h, user, q)
    require_human(user)
    with DB_LOCK:
        rows = db().execute("SELECT * FROM invites WHERE project_id = ? AND used_ts IS NULL AND expires_ts > ? "
                            "AND (? OR created_by=?) ORDER BY created_ts DESC",
                            (p["id"], time.time(), p["role"] == "owner", user["name"])).fetchall()
    return {"invites": [{"name": r["name"], "kind": r["kind"], "purpose": r["purpose"], "by": account_name(r["created_by"]),
                         "expires": iso(r["expires_ts"])} for r in rows]}


@route("POST", "/api/invites/cancel")
def api_invite_cancel(h, user, q, data):
    p = project_for(h, user, q)
    require_human(user)
    with DB_LOCK:
        n = db().execute("DELETE FROM invites WHERE project_id = ? AND name = ? AND used_ts IS NULL "
                         "AND (? OR created_by=?)",
                         (p["id"], str(data.get("name") or ""), p["role"] == "owner", user["name"])).rowcount
    return {"ok": True, "cancelled": n}


@route("GET", "/api/invite", auth=False)
def api_invite_info(h, user, q, data):
    inv = live_invite(q.get("code"))
    return {"name": inv["name"], "kind": inv["kind"], "owner": account_name(inv["owner"]), "purpose": inv["purpose"],
            "project": inv["project_slug"], "project_title": inv["project_title"], "expires": iso(inv["expires_ts"])}


@route("POST", "/api/join", auth=False)
def api_join(h, user, q, data):
    me = None
    if h.headers.get("Authorization"):
        me = h.auth()
    return redeem_invite(str(data.get("code") or ""), data.get("password"), h.headers.get("User-Agent", ""), me,
                         bool(data.get("as_existing")))


# ---- projects and their members
@route("GET", "/api/projects")
def api_projects(h, user, q, data):
    """Unread state belongs to the account; legacy ?seen additionally narrows the count."""
    seen = {}
    for part in str(q.get("seen") or "").split(","):
        slug, _, mid = part.partition(":")
        if mid.isdigit():
            seen[slug] = int(mid)
    with DB_LOCK:
        out = []
        for p in my_projects(user["name"]):
            last = db().execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE project_id = ?", (p["id"],)).fetchone()[0]
            n = db().execute("SELECT COUNT(*) FROM members WHERE project_id = ?", (p["id"],)).fetchone()[0]
            d = dict(project_dict(p, user), last_id=last, members=n)
            stats = db().execute("SELECT COUNT(*), MIN(m.id) FROM messages m WHERE m.conversation_id IS NULL AND m.project_id=? AND m.id>? AND m.author!=? "
                                 "AND m.deleted_ts IS NULL AND m.type NOT IN ('status','approval') AND NOT EXISTS "
                                 "(SELECT 1 FROM receipts r WHERE r.message_id=m.id AND r.user=? AND r.read_ts IS NOT NULL)",
                                 (p["id"], max(seen.get(p["slug"], 0), d["read_upto"]), user["name"], user["name"])).fetchone()
            d.update(unread=stats[0], first_unread_id=stats[1])
            private_unread = 0
            for c in db().execute("SELECT * FROM conversations WHERE project_id=? AND (human=? OR agent=?)", (p["id"], user["name"], user["name"])):
                try:
                    conversation_row(c["id"], user, p["id"])
                    private_unread += conversation_dict(c, user)["unread"]
                except ApiError:
                    pass
            d["direct_unread"] = private_unread
            out.append(d)
    return {"projects": out}


@route("POST", "/api/projects/order")
def api_project_order(h, user, q, data):
    if user["kind"] != "human":
        raise ApiError(403, _ui_text('порядок проектов выбирает человек'))
    ids = data.get("project_ids")
    if set(data) != {"project_ids"} or not isinstance(ids, list) or any(type(x) is not int for x in ids) or len(ids) != len(set(ids)):
        raise ApiError(400, _ui_text('project_ids: список разных идентификаторов проектов'))
    with DB_LOCK:
        con = db()
        current = [p["id"] for p in my_projects(user["name"])]
        if not set(ids).issubset(current):
            raise ApiError(404, _ui_text('проект не найден'))
        # Concurrently added memberships remain visible at the end of the list.
        order = ids + [pid for pid in current if pid not in ids]
        con.execute("BEGIN IMMEDIATE")
        try:
            con.executemany("UPDATE members SET sidebar_position=? WHERE user=? AND project_id=?",
                            [(i, user["name"], pid) for i, pid in enumerate(order)])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return {"project_ids": order}


@route("POST", "/api/projects")
def api_project_create(h, user, q, data):
    if user["kind"] != "human":
        raise ApiError(403, _ui_text('проекты создают люди, не агенты'))
    title = project_title(data)
    slug = str(data.get("slug") or "").strip().lower() or slugify(title)
    if not SLUG_RE.match(slug):
        raise ApiError(400, _ui_text('короткое имя проекта: латиница, цифры и -, от 2 до 40 символов'))
    with DB_LOCK:
        con = db()
        pid = create_project_row(con, slug, title, user["name"])
        add_member(con, pid, user["name"], "owner", user["name"])
        p = con.execute("SELECT p.*, 'owner' AS role FROM projects p WHERE id = ?", (pid,)).fetchone()
    return {"project": project_dict(p, user)}


@route("POST", "/api/project/rename")
def api_project_rename(h, user, q, data):
    with DB_LOCK:
        p = project_for(h, user, q)
        if not can_rename_project(p, user):
            raise ApiError(403, _ui_text('переименовать проект может только его создатель — человек из этого проекта'))
        title = project_title(data)
        db().execute("UPDATE projects SET title = ? WHERE id = ?", (title, p["id"]))
        result = project_dict(p, user)
        result["title"] = title
    return {"ok": True, "project": result}


@route("GET", "/api/members")
def api_members(h, user, q, data):
    p = project_for(h, user, q)
    with DB_LOCK:
        return {"project": project_dict(p, user), "members": users_with_presence(p["id"])}


@route("POST", "/api/project/delete")
def api_project_delete(h, user, q, data):
    """Owner only, confirmed by typing the project's name (title or slug). Everything of the project goes;
    its agents stay as accounts without a project (their person can add them to another one)."""
    p = project_for(h, user, q)
    require_owner(p)
    typed = str(data.get("confirm") or "").strip().lower()
    if typed not in (p["slug"].lower(), p["title"].strip().lower()):
        raise ApiError(400, f"{_ui_text('для подтверждения введи название проекта: ')}{p['title']}")
    with UPLOAD_LOCK, DB_LOCK:
        con = db()
        pid = p["id"]
        uploaded_files = [r["id"] for r in con.execute("SELECT id FROM file_uploads WHERE project_id=?", (pid,))]
        agents = [account_name(r["user"]) for r in con.execute("SELECT user FROM members WHERE project_id = ? AND role = 'agent'", (pid,))]
        n = con.execute("SELECT COUNT(*) FROM messages WHERE project_id = ?", (pid,)).fetchone()[0]
        con.execute("BEGIN IMMEDIATE")
        try:
            for table in ("attachments", "receipts", "waiting_dismissals", "approvals", "reactions", "secret_envelopes"):
                con.execute(f"DELETE FROM {table} WHERE message_id IN (SELECT id FROM messages WHERE project_id = ?)", (pid,))
            for table in ("messages", "board", "claims", "invites", "members", "conversations"):
                con.execute(f"DELETE FROM {table} WHERE project_id = ?", (pid,))
            con.execute("DELETE FROM file_uploads WHERE project_id=?", (pid,))
            con.execute("DELETE FROM projects WHERE id = ?", (pid,))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        for uid in uploaded_files:
            try:
                os.remove(upload_path(uid))
            except FileNotFoundError:
                pass
    return {"ok": True, "deleted": p["slug"], "messages": n, "agents_without_project": agents}


def addable(con, me, pid):
    """Who a project's owner may add directly, without a link: people who already share a project with
    them, and their own agents that have no project (an agent lives in one project)."""
    people = con.execute(
        "SELECT u.name, GROUP_CONCAT(DISTINCT p.title) AS via FROM members mine "
        "JOIN members other ON other.project_id = mine.project_id AND other.user != mine.user "
        "JOIN users u ON u.name = other.user AND u.kind = 'human' AND u.revoked = 0 "
        "JOIN projects p ON p.id = mine.project_id "
        "WHERE mine.user = ? AND NOT EXISTS (SELECT 1 FROM members x WHERE x.project_id = ? AND x.user = u.name) "
        "GROUP BY u.name ORDER BY u.name", (me["name"], pid)).fetchall()
    agents = con.execute(
        "SELECT name FROM users u WHERE kind = 'agent' AND owner = ? AND revoked = 0 "
        "AND NOT EXISTS (SELECT 1 FROM members x WHERE x.user = u.name) ORDER BY name", (me["name"],)).fetchall()
    return ([{"id": r["name"], "name": account_name(r["name"]), "kind": "human", "via": (r["via"] or "").split(",")} for r in people] +
            [{"id": r["name"], "name": account_name(r["name"]), "kind": "agent", "via": []} for r in agents])


@route("GET", "/api/members/candidates")
def api_member_candidates(h, user, q, data):
    p = project_for(h, user, q)
    require_human(user)
    if p["admin_owner"]:
        return {"candidates": []}
    with DB_LOCK:
        return {"candidates": [c for c in addable(db(), user, p["id"])
                               if p["role"] == "owner" or c["kind"] == "agent"]}


@route("POST", "/api/members/add")
def api_member_add(h, user, q, data):
    p = project_for(h, user, q)
    require_human(user)
    name = str(data.get("name") or "")
    with DB_LOCK:
        con = db()
        candidates = [c for c in addable(con, user, p["id"])
                      if c["id"] == data["account_id"]] if data.get("account_id") else \
                     [c for c in addable(con, user, p["id"]) if c["name"].lower() == name.lower()]
        if len(candidates) > 1:
            raise ApiError(409, _ui_text('имя неоднозначно: выбери аккаунт по account_id'))
        ok = candidates[0] if candidates else None
        if not ok or (p["role"] != "owner" and ok["kind"] != "agent"):
            raise ApiError(403, f"{_ui_text('напрямую добавляются только люди из твоих общих проектов и твои агенты без проекта; ')}{name}{_ui_text(' пригласи ссылкой')}")
        add_member(con, p["id"], ok["id"], "agent" if ok["kind"] == "agent" else "member", user["name"])
        name = ok["name"]
    insert_message(user, h.host(), {"body": f"Добавил(а) {name} в проект.", "type": "info"}, p["id"])
    return {"ok": True}


def owners_left(pid, without):
    return db().execute("SELECT COUNT(*) FROM members WHERE project_id = ? AND role = 'owner' AND user != ?",
                        (pid, without)).fetchone()[0]


@route("POST", "/api/members/remove")
def api_member_remove(h, user, q, data):
    p = project_for(h, user, q)
    with DB_LOCK:
        name = resolve_account(data.get("name"), p["id"], account_id=data.get("account_id"))["name"]
        if name != user["name"]:
            require_owner(p)     # anyone may leave; only owners remove others
        role = member_role(p["id"], name)
        if not role:
            raise ApiError(404, f"{name}{_ui_text(' нет в проекте')}")
        if role == "owner" and not owners_left(p["id"], name):
            raise ApiError(409, _ui_text('это последний владелец: сначала сделай владельцем кого-то ещё'))
        if p["service_agent"] == name:
            db().execute("UPDATE projects SET service_agent=NULL WHERE id=?", (p["id"],))
        db().execute("DELETE FROM members WHERE project_id = ? AND user = ?", (p["id"], name))
        # their agents in this project go too: an agent without its person makes no sense here
        db().execute("DELETE FROM members WHERE project_id = ? AND user IN "
                     "(SELECT name FROM users WHERE kind = 'agent' AND owner = ?)", (p["id"], name))
        db().execute("DELETE FROM invites WHERE project_id=? AND kind='agent' AND owner=? AND used_ts IS NULL",
                     (p["id"], name))
    return {"ok": True}


def rename_account(con, key, new):
    """Only the label changes; auth, history, ownership and invitations keep their account key."""
    old = account_name(key)
    con.execute("UPDATE users SET display_name=? WHERE name=?", (new, key))
    con.execute("UPDATE invites SET name=? WHERE account_id=?", (new, key))
    _login_fails.pop(f"name:{old.lower()}", None)


@route("POST", "/api/rename")
def api_rename(h, user, q, data):
    if user["kind"] != "human":
        raise ApiError(403, _ui_text('переименовывает человек: себя или своих агентов'))
    new = str(data.get("new") or "").strip()
    if not NAME_RE.fullmatch(new):
        raise ApiError(400, _ui_text('имя: латиница, цифры, . _ -, до 32 символов'))
    with DB_LOCK:
        con = db()
        target = managed_account(h, user, q, data)
        key, old = target["name"], account_name(target["name"])
        if not (key == user["name"] or (target["kind"] == "agent" and target["owner"] == user["name"]) or user["is_admin"]):
            raise ApiError(403, _ui_text('переименовать можно себя и своих агентов'))
        if new == old:
            return {"ok": True, "id": key, "name": new}
        if target["kind"] == "human" and any(u["name"] != key for u in find_accounts(new, kind="human")):
            raise ApiError(409, _ui_text('имя человека уже занято'))
        pids = [r["project_id"] for r in con.execute("SELECT project_id FROM members WHERE user=?", (key,))]
        for pid in pids:
            check_member_name(con, pid, new, key)
        pending = con.execute("SELECT kind,project_id FROM invites WHERE lower(name)=lower(?) AND purpose='new' "
                              "AND used_ts IS NULL AND expires_ts>?", (new, time.time())).fetchall()
        if any((target["kind"] == "human" and r["kind"] == "human") or r["project_id"] in pids for r in pending):
            raise ApiError(409, _ui_text('имя занято действующим приглашением'))
        con.execute("BEGIN IMMEDIATE")
        try:
            rename_account(con, key, new)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        me = con.execute("SELECT * FROM users WHERE name=?", (user["name"],)).fetchone()
    who = f"Теперь меня зовут {new} (было {old})." if key == user["name"] else f"Агент {old} теперь называется {new}."
    for pid in pids:
        insert_message(me, h.host(), {"body": who + " Пишите по новому имени (--to " + new + ").", "type": "info"}, pid)
    return {"ok": True, "id": key, "name": new}


@route("POST", "/api/members/role")
def api_member_role(h, user, q, data):
    p = project_for(h, user, q)
    require_owner(p)
    name, role = str(data.get("name") or ""), data.get("role")
    if role not in ("owner", "member"):
        raise ApiError(400, "role: owner | member")
    with DB_LOCK:
        name = resolve_account(name, p["id"], account_id=data.get("account_id"))["name"]
        cur = member_role(p["id"], name)
        if cur not in ("owner", "member"):
            raise ApiError(404 if not cur else 400, f"{name} нет в проекте" if not cur else "роль бывает только у людей")
        if cur == "owner" and role == "member" and not owners_left(p["id"], name):
            raise ApiError(409, _ui_text('это последний владелец'))
        db().execute("UPDATE members SET role = ? WHERE project_id = ? AND user = ?", (role, p["id"], name))
    return {"ok": True}


@route("GET", "/api/summary")
def api_summary(h, user, q, data):
    """One round trip for agent hooks: am I listening, and what is still unacked."""
    p = project_for(h, user, q)
    if q.get("client") == "codex":
        touch(user, checking=True)
    with DB_LOCK:
        me = presence(user)
        unacked = list_messages(user, {"for_me": "1", "unacked": "1", "full": "0", "limit": "1000"}, p["id"])
        shown = unacked[-max(1, min(int(q.get("limit") or 10), 50)):]
        if shown:
            mark(user, [m["id"] for m in shown], "delivered")
        claim_fields = ("id", "owner", "created", "expired", "expires_in") if q.get("metadata") == "1" else \
                       ("id", "key", "owner", "topic", "until", "created", "expired", "expires_in")
        claims = [{k: c[k] for k in claim_fields} for c in list_claims({"active": "1"}, p["id"])]
        wait = waiting_summary(user["name"], p["id"])
    if q.get("metadata") == "1":
        shown = [{k: m[k] for k in ("id", "author", "type", "priority", "read")} for m in shown]
    return {"name": account_name(user["name"]), "listening": me["listening"], "last_listen": me["last_listen"],
            "unacked_total": len(unacked), "unacked": shown, "last_id": LAST_ID[0], "claims": claims, **wait}


@route("GET", "/api/messages")
def api_list(h, user, q, data):
    p = project_for(h, user, q)
    with DB_LOCK:
        msgs = list_messages(user, q, p["id"])
        if msgs and (q.get("for_me") == "1" or user["kind"] == "human"):
            mark(user, [m["id"] for m in msgs], "delivered")
        return {"messages": msgs, "last_id": LAST_ID[0], "reaction_revision": reaction_revision(p["id"]),
                "receipt_revision": receipt_revision(p["id"]), "content_revision": content_revision(p["id"],user["name"])}


@route("GET", "/api/conversations")
def api_conversations(h, user, q, data):
    p = project_for(h, user, q)
    with DB_LOCK:
        out = []
        for c in db().execute("SELECT * FROM conversations WHERE project_id=? AND (human=? OR agent=?)", (p["id"], user["name"], user["name"])):
            try:
                conversation_row(c["id"], user, p["id"])
                out.append(conversation_dict(c, user))
            except ApiError:
                pass
        return {"conversations": sorted(out, key=lambda c: c["last_ts"], reverse=True)}


@route("POST", "/api/conversations")
def api_conversation_create(h, user, q, data):
    p = project_for(h, user, q)
    with DB_LOCK:
        peer = db().execute("SELECT * FROM users WHERE name=? AND revoked=0", (data.get("peer_id"),)).fetchone()
        if not peer or not member_role(p["id"], peer["name"]):
            raise ApiError(404, _ui_text('нет такого участника'))
        if user["kind"] == peer["kind"]:
            raise ApiError(400, _ui_text('личный чат связывает человека и агента своего проекта'))
        human, agent = (user, peer) if user["kind"] == "human" else (peer, user)
        db().execute("INSERT OR IGNORE INTO conversations(id,project_id,human,agent,created) VALUES (?,?,?,?,?)",
                     ("d_" + secrets.token_hex(24), p["id"], human["name"], agent["name"], time.time()))
        c = db().execute("SELECT * FROM conversations WHERE project_id=? AND human=? AND agent=?", (p["id"], human["name"], agent["name"])).fetchone()
        return {"conversation": conversation_dict(c, user)}


@route("POST", r"/api/conversations/(d_[0-9a-f]{48})/secrets")
def api_secret_send(h, user, q, data, cid):
    p = project_for(h, user, q)
    with UPLOAD_LOCK, DB_LOCK:
        c = conversation_row(cid, user, p["id"])
        if user["name"] != c["human"]:
            raise ApiError(403, _ui_text('секрет адресуется агенту от человека личного чата'))
        idem = data.get("idem_key")
        if not isinstance(idem, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,100}", idem):
            raise ApiError(400, _ui_text('нужен уникальный idem_key от 16 до 100 символов'))
        duplicate = db().execute("SELECT * FROM messages WHERE author=? AND idem_key=?", (user["name"],idem)).fetchone()
        if duplicate:
            if duplicate["conversation_id"]!=cid or not db().execute("SELECT 1 FROM secret_envelopes WHERE message_id=?",(duplicate["id"],)).fetchone():
                raise ApiError(409,_ui_text('ключ сообщения использован в другом сообщении'))
            return {"id":duplicate["id"],"duplicate":True}
        value=data.get("value")
        if not isinstance(value,str) or not value or len(value.encode())>32768:
            raise ApiError(400,_ui_text('секрет: непустой текст до 32 КиБ'))
        ttl = data.get("expires_in", 3600)
        if type(ttl) is not int or not 300 <= ttl <= 86400:
            raise ApiError(400, _ui_text('срок секрета: от 5 минут до суток'))
        require_file_space(65536)
        aad=json.dumps(["telegent-secret-v1",cid,c["human"],c["agent"],idem],ensure_ascii=False,separators=(",",":")).encode()
        envelope=seal_secret(value.encode(),aad)
        mid, dup = insert_message(user, h.host(), {"body": "🔐 Передан секрет", "conversation_id": cid, "idem_key": idem}, p["id"],
                                  secret={"key_id":envelope["key_id"], "envelope":envelope, "expires":time.time()+ttl})
    return {"id":mid,"duplicate":dup}


@route("POST", r"/api/messages/(\d+)/secret")
def api_secret_fetch(h, user, q, data, mid):
    with DB_LOCK:
        m = get_message_row(mid, user)
        secret = db().execute("SELECT * FROM secret_envelopes WHERE message_id=?", (mid,)).fetchone()
        if not secret:
            raise ApiError(404, _ui_text('нет такого секрета'))
        action = data.get("action", "fetch")
        if action == "revoke":
            if m["author"] != user["name"]:
                raise ApiError(403, _ui_text('отозвать секрет может отправитель'))
            if secret["consumed"]:
                raise ApiError(410, _ui_text('секрет уже получен: отзыв не удалит его у получателя'))
            if secret["claimed_ts"]:
                raise ApiError(409, _ui_text('получение уже началось: отзыв не удалит файл у получателя'))
            db().execute("UPDATE secret_envelopes SET envelope=NULL,revoked=COALESCE(revoked,?) WHERE message_id=?", (time.time(), mid))
            return {"ok": True}
        if secret["recipient"] != user["name"]:
            raise ApiError(403, _ui_text('получить секрет может только адресованный агент'))
        claim = data.get("claim_key")
        if not isinstance(claim, str) or not re.fullmatch(r"[0-9a-f]{64}", claim):
            raise ApiError(400, _ui_text('нужен постоянный claim_key из 64 шестнадцатеричных знаков для безопасного повтора'))
        claim = token_hash(claim)
        if action == "consume" and secret["consumed"] and secret["claimed_by"] == claim:
            return {"ok": True}
        if action == "consume" and secret["claimed_by"] == claim and not secret["revoked"] and not secret["consumed"]:
            # A durable save may be confirmed after expiry; this never returns the value again.
            db().execute("UPDATE secret_envelopes SET envelope=NULL,consumed=? WHERE message_id=?", (time.time(), mid))
            mark(user, [mid], "read")
            return {"ok": True}
        require_active_message(m)
        if secret["revoked"] or secret["consumed"] or secret["expires"] <= time.time():
            db().execute("UPDATE secret_envelopes SET envelope=NULL WHERE message_id=?", (mid,))
            raise ApiError(410, _ui_text('секрет получен, отозван или срок его действия истёк'))
        if secret["claimed_by"] and secret["claimed_by"] != claim:
            raise ApiError(409, _ui_text('получение уже начато: продолжи прежнюю команду на том же устройстве'))
        if action == "consume":
            if secret["claimed_by"] != claim:
                raise ApiError(409, _ui_text('сначала получи пакет'))
            db().execute("UPDATE secret_envelopes SET envelope=NULL,consumed=? WHERE message_id=?", (time.time(), mid))
            mark(user, [mid], "read")
            return {"ok": True}
        if action != "fetch":
            raise ApiError(400, "action: fetch | consume | revoke")
        db().execute("UPDATE secret_envelopes SET claimed_by=?,claimed_ts=COALESCE(claimed_ts,?) WHERE message_id=?", (claim, time.time(), mid))
        aad=json.dumps(["telegent-secret-v1",m["conversation_id"],m["author"],secret["recipient"],m["idem_key"]],ensure_ascii=False,separators=(",",":")).encode()
        raw=unseal_secret(json.loads(secret["envelope"]),aad)
        return {"content_b64":base64.b64encode(raw).decode()}


@route("POST", "/api/messages")
def api_send(h, user, q, data):
    p = project_for(h, user, q)
    mid, dup = insert_message(user, h.host(), data, p["id"])
    warnings, released = [], None
    claim = norm_key(data.get("claim"))
    with DB_LOCK:
        if claim and not dup:
            if data.get("type") != "result":
                warnings.append("claim учитывается только у type=result — заявка не снята")
            else:
                row = db().execute("SELECT * FROM claims WHERE key = ? AND owner = ? AND released IS NULL "
                                   "AND project_id = ?", (claim, user["name"], p["id"])).fetchone()
                if row:
                    released = release_claim(row, None, mid)
                else:
                    warnings.append(f"у тебя нет активной заявки «{str(data['claim'])[:80]}» — сообщение принято, снимать нечего")
        m = get_message_row(mid)
        expected = set(expected_recipients(p["id"], user["name"], recipients_list(m["recipients"])))
        offline = [dict(id=u["id"], name=u["name"], last_listen=u["last_listen"], last_check=u["last_check"]) for u in users_with_presence(p["id"])
                   if u["id"] in expected and not u["listening"]]
        for o in offline:     # the recipient's board entry tells when they may be back
            b = db().execute("SELECT * FROM board WHERE project_id = ? AND name = ?", (p["id"], o["id"])).fetchone()
            if b and b["ts"]:
                o.update(board_now=b["now"], board_back=b["back"], board_age=round(time.time() - b["ts"]))
    res = {"id": mid, "duplicate": dup, "not_listening": offline}
    if warnings:
        res["warnings"] = warnings
    if released:
        res["released_claim"] = released
    return res


@route("GET", r"/api/messages/(\d+)")
def api_get(h, user, q, data, mid):
    with DB_LOCK:
        m = get_message_row(mid, user)
        if q.get("mark") in ("read", "ack"):
            mark(user, [mid], q["mark"])
        msg = msg_dicts([m], user["name"])[0]
        thread = list_messages(user, {"thread": m["thread_id"], "limit": 500, "after": "0", "conversation": m["conversation_id"]}, m["project_id"])
    return {"message": msg, "thread": thread}


def reaction_revision(pid):
    return REACTION_EPOCH + ":" + str(REACTION_REVISIONS.get(pid, 0))


def receipt_revision(pid):
    return REACTION_EPOCH + ":" + str(RECEIPT_REVISIONS.get(pid, 0))


def content_revision(pid, who):
    return REACTION_EPOCH + ":" + str(CONTENT_REVISIONS.get((pid,who),0))


def require_active_message(message):
    if message["deleted_ts"]:
        raise ApiError(410,_ui_text('сообщение удалено'))


def publish_message_change(message):
    con,pid=db(),message["project_id"]
    if message["conversation_id"]:
        c=con.execute("SELECT human,agent FROM conversations WHERE id=?",(message["conversation_id"],)).fetchone()
        viewers=list(c) if c else []
    else:
        viewers=[r[0] for r in con.execute("SELECT user FROM members WHERE project_id=?",(pid,))]
    with NEW_MSG:
        for who in viewers:
            key=(pid,who);CONTENT_REVISIONS[key]=CONTENT_REVISIONS.get(key,0)+1
        NEW_MSG.notify_all()


def mutate_message(h,user,q,data,mid,deleting=False):
    p=project_for(h,user,q)
    with DB_LOCK:
        message=get_message_row(mid,user)
        if message["project_id"]!=p["id"]:
            raise ApiError(404,_ui_text('нет сообщения в выбранном проекте'))
        if user["kind"]!="human" or message["author"]!=user["name"] or message["author_kind"]!="human" or message["type"] not in USER_TYPES:
            raise ApiError(403,_ui_text('человек может изменить или удалить только собственное сообщение'))
        if not isinstance(data,dict) or set(data)-({"revision"} if deleting else {"revision","body"}) or type(data.get("revision")) is not int:
            raise ApiError(400,_ui_text('нужны текущая revision и текст body для изменения'))
        if message["deleted_ts"]:
            if not deleting:require_active_message(message)
            return {"ok":True,"message":msg_dicts([message],user["name"])[0]}
        body=data.get("body")
        if not deleting:
            if db().execute("SELECT 1 FROM secret_envelopes WHERE message_id=?",(mid,)).fetchone():
                raise ApiError(400,_ui_text('карточка секрета не редактируется'))
            if not isinstance(body,str):raise ApiError(400,_ui_text('body: текст сообщения'))
            try:body.encode("utf-8")
            except UnicodeError:raise ApiError(400,_ui_text('текст содержит недопустимый Unicode'))
            if len(body)>MAX_BODY_CHARS:raise ApiError(413,_ui_text('текст слишком длинный'))
            if not body.strip() and not db().execute("SELECT 1 FROM attachments WHERE message_id=?",(mid,)).fetchone():
                raise ApiError(400,_ui_text('пустое сообщение'))
            check_secret("текст",body)
        if data["revision"]!=message["revision"]:
            if deleting or body!=message["body"]:
                raise ApiError(409,_ui_text('сообщение уже изменилось; закрой редактор и открой актуальную версию'))
        if not deleting and body==message["body"]:
            return {"ok":True,"message":msg_dicts([message],user["name"])[0]}
        con,now=db(),time.time()
        con.execute("BEGIN IMMEDIATE")
        try:
            # A decision authorizes one text version; preserve its record without reusing it.
            digest=hashlib.sha256(message["body"].encode()).hexdigest()
            con.execute("INSERT OR IGNORE INTO approval_history(message_id,revision,user,decision,note,ts,body_hash) "
                        "SELECT message_id,?,user,decision,note,ts,? FROM approvals WHERE message_id=?",(message["revision"],digest,mid))
            con.execute("DELETE FROM approvals WHERE message_id=?",(mid,))
            if deleting:
                con.execute("INSERT OR IGNORE INTO pending_file_deletions(upload_id) SELECT upload_id FROM attachments WHERE message_id=? AND upload_id IS NOT NULL",(mid,))
                con.execute("DELETE FROM attachments WHERE message_id=?",(mid,))
                con.execute("UPDATE messages SET body='',subject='Сообщение удалено',topic='',result=NULL,needs_approval=0,"
                            "status=NULL,status_ref=NULL,status_note=NULL,deleted_ts=?,revision=revision+1 WHERE id=?",(now,mid))
                con.execute("UPDATE secret_envelopes SET envelope=NULL,revoked=CASE WHEN claimed_ts IS NULL THEN COALESCE(revoked,?) ELSE revoked END WHERE message_id=?",(now,mid))
            else:
                subject=next((line.strip() for line in body.splitlines() if line.strip()),"")[:120]
                con.execute("UPDATE messages SET body=?,subject=?,edited_ts=?,revision=revision+1 WHERE id=?",(body,subject,now,mid))
                if message["type"]=="decision":
                    con.execute("UPDATE messages SET status='open',status_by=NULL,status_ts=NULL WHERE id=?",(mid,))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK");raise
        publish_message_change(message)
        return {"ok":True,"message":msg_dicts([get_message_row(mid,user)],user["name"])[0]}


@route("POST", r"/api/messages/(\d+)/edit")
def api_message_edit(h,user,q,data,mid):
    return mutate_message(h,user,q,data,mid)


@route("POST", r"/api/messages/(\d+)/delete")
def api_message_delete(h,user,q,data,mid):
    result=mutate_message(h,user,q,data,mid,deleting=True)
    purge_deleted_files()
    return result


def purge_deleted_files():
    # Commit visibility first; failed filesystem cleanup remains durable and is retried.
    with UPLOAD_LOCK, DB_LOCK:
        con=db()
        for row in con.execute("SELECT upload_id FROM pending_file_deletions").fetchall():
            uid=row["upload_id"]
            try:os.remove(upload_path(uid))
            except FileNotFoundError:pass
            except OSError:continue
            con.execute("DELETE FROM file_uploads WHERE id=?",(uid,))
            con.execute("DELETE FROM pending_file_deletions WHERE upload_id=?",(uid,))


@route("POST", r"/api/messages/(\d+)/reaction")
def api_reaction(h, user, q, data, mid):
    with DB_LOCK:
        message = get_message_row(mid, user)
        require_active_message(message)
        if not isinstance(data, dict):
            raise ApiError(400, _ui_text('нужны emoji и active (true или false)'))
        emoji, active = data.get("emoji"), data.get("active", True)
        if emoji == "❤":
            emoji = "❤️"
        if emoji == "+1":
            emoji = "👍"
        if emoji not in REACTION_EMOJI or type(active) is not bool:
            raise ApiError(400, _ui_text('реакция: ❤️, 🔥, 👍, 👎, 🤡, 😁, 😭; active: true | false'))
        if message["type"] in SYSTEM_TYPES:
            raise ApiError(400, _ui_text('реакции доступны у сообщений участников'))
        con = db()
        if active:
            changed = con.execute("INSERT OR IGNORE INTO reactions(message_id,user,emoji,ts) VALUES (?,?,?,?)",
                                  (mid, user["name"], emoji, time.time())).rowcount
        else:
            changed = con.execute("DELETE FROM reactions WHERE message_id=? AND user=? AND emoji=?",
                                  (mid, user["name"], emoji)).rowcount
        if changed:
            with NEW_MSG:
                pid = message["project_id"]
                REACTION_REVISIONS[pid] = REACTION_REVISIONS.get(pid, 0) + 1
                NEW_MSG.notify_all()
        return {"ok": True, "reactions": msg_dicts([message], user["name"])[0]["reactions"]}


@route("POST", r"/api/messages/(\d+)/status")
def api_status(h, user, q, data, mid):
    sid, _ = set_status(user, h.host(), mid, data)
    return {"ok": True, "status_message": sid}


@route("POST", r"/api/messages/(\d+)/approval")
def api_approval(h, user, q, data, mid):
    sid, _ = set_approval(user, h.host(), mid, data)
    return {"ok": True, "approval_message": sid}


@route("POST", "/api/waiting/dismiss")
def api_dismiss_waiting(h, user, q, data):
    if user["kind"] != "human":
        raise ApiError(403, _ui_text('только для человека'))
    if set(data) - {"ids", "all"} or ("all" in data and data["all"] is not True):
        raise ApiError(400, "ids | all=true")
    ids = data.get("ids")
    if (data.get("all") and ids is not None) or (not data.get("all") and
            (not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or
             any(type(mid) is not int or not 0 < mid < 2**63 for mid in ids))):
        raise ApiError(400, "ids: 1..1000 | all=true")
    p = project_for(h, user, q)
    with DB_LOCK:
        visible, va = message_visibility(user["name"])
        args = [p["id"], user["name"], f"%,{user['name']},%", user["name"], user["name"]] + va
        where = f"m.project_id=? AND {WAITING_SQL} AND {visible}"
        if ids is not None:
            # Validate the whole batch before changing the caller's reminders.
            for mid in ids:
                if get_message_row(mid, user)["project_id"] != p["id"]:
                    raise ApiError(404, _ui_text('нет доступа к проекту'))
            where += f" AND m.id IN ({','.join('?' for _ in ids)})"
            args += ids
        rows = db().execute(f"SELECT m.id,m.revision FROM messages m WHERE {where}", args).fetchall()
        db().executemany("INSERT OR REPLACE INTO waiting_dismissals(user,message_id,revision) VALUES (?,?,?)",
                         [(user["name"], row["id"], row["revision"]) for row in rows])
        if rows:
            with NEW_MSG:
                RECEIPT_REVISIONS[p["id"]] = RECEIPT_REVISIONS.get(p["id"], 0) + 1
                NEW_MSG.notify_all()
    return {"ok": True, "dismissed": len(rows)}


@route("POST", "/api/receipts")
def api_receipts(h, user, q, data):
    kind = data.get("kind", "read")
    if kind not in ("read", "ack"):
        raise ApiError(400, "kind: read | ack")
    ids = [int(x) for x in data.get("ids") or []]
    with DB_LOCK:
        cursor = None
        if "seen_upto" in data:
            if kind != "read" or type(data["seen_upto"]) is not int or not 0 <= data["seen_upto"] < 2**63:
                raise ApiError(400, _ui_text('seen_upto: целый неотрицательный номер, только вместе с kind=read'))
            p = project_for(h, user, q)
            # Never pre-read messages that do not exist yet, or use another project's cursor.
            if q.get("conversation"):
                c = conversation_row(q["conversation"], user, p["id"])
                col = "human_read_upto" if user["name"] == c["human"] else "agent_read_upto"
                latest = db().execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE conversation_id=? AND id<=?", (c["id"], data["seen_upto"])).fetchone()[0]
                changed = db().execute(f"UPDATE conversations SET {col}=MAX({col},?) WHERE id=? AND {col}<?", (latest,c["id"],latest)).rowcount
                cursor = db().execute(f"SELECT {col} FROM conversations WHERE id=?", (c["id"],)).fetchone()[0]
            else:
                latest = db().execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE project_id=? AND conversation_id IS NULL AND id<=?",
                                      (p["id"], data["seen_upto"])).fetchone()[0]
                changed = db().execute("UPDATE members SET read_upto=? WHERE project_id=? AND user=? AND read_upto<?",
                                       (latest, p["id"], user["name"], latest)).rowcount
                cursor = db().execute("SELECT read_upto FROM members WHERE project_id=? AND user=?", (p["id"], user["name"])).fetchone()[0]
            if changed:
                with NEW_MSG:
                    RECEIPT_REVISIONS[p["id"]] = RECEIPT_REVISIONS.get(p["id"], 0) + 1
                    NEW_MSG.notify_all()
        if data.get("all_for_me_upto"):
            p = project_for(h, user, q)
            ids = [m["id"] for m in list_messages(user, {"for_me": "1", "unacked": "1", "limit": 1000, "full": "0"}, p["id"])
                   if m["id"] <= int(data["all_for_me_upto"])]
        else:   # only messages of projects the user is in
            ids = [i for i in ids if can_read_message(db().execute("SELECT * FROM messages WHERE id = ?", (i,)).fetchone(), user)]
        mark(user, ids, kind)
    return {"ok": True, "ids": ids, **({"read_upto": cursor} if cursor is not None else {})}


@route("POST", "/api/activity")
def api_activity(h, user, q, data):
    if user["kind"] != "agent":
        raise ApiError(403, _ui_text("активность сообщает клиент агента"))
    project_for(h, user, q)
    if set(data) != {"state", "client"} or data.get("state") not in ("working", "idle") or not isinstance(data.get("client"), str) or not re.fullmatch(r"[0-9a-f]{32}", data["client"]):
        raise ApiError(400, _ui_text("активность: state working/idle и непрозрачная метка клиента"))
    now = time.time()
    with _activity_lock:
        clients = _agent_activity.setdefault(user["name"], {})
        if data["client"] not in clients and len(clients) >= 16:
            del clients[min(clients, key=lambda k: clients[k][1])]
        clients[data["client"]] = (data["state"], now)
    return {"ok": True}


@route("GET", "/api/wait")
def api_wait(h, user, q, data):
    after = int(q.get("after") or 0)
    timeout = max(0.0, min(float(q.get("timeout") or 25), 55))
    deadline = time.time() + timeout
    qq = {"after": str(after), "limit": "200", "for_me": q.get("for_me", "1"), "full": q.get("full", "1")}
    if q.get("conversation"):
        qq["conversation"] = q["conversation"]
    p = project_for(h, user, q)
    touch(user, waiting=True)
    while True:
        with NEW_MSG:
            seen = LAST_ID[0]
            revision = reaction_revision(p["id"])
            receipts = receipt_revision(p["id"])
            content = content_revision(p["id"],user["name"])
        with DB_LOCK:
            msgs = list_messages(user, qq, p["id"])
            if msgs and (qq["for_me"] == "1" or user["kind"] == "human"):
                mark(user, [m["id"] for m in msgs], "delivered")
        if (msgs or ("reaction_revision" in q and q["reaction_revision"] != revision)
                or ("receipt_revision" in q and q["receipt_revision"] != receipts)
                or ("content_revision" in q and q["content_revision"] != content)):
            return {"messages": msgs, "last_id": seen, "reaction_revision": revision,
                    "receipt_revision": receipt_revision(p["id"]),"content_revision":content_revision(p["id"],user["name"])}
        remaining = deadline - time.time()
        if remaining <= 0:
            return {"messages": [], "last_id": max(seen, after), "reaction_revision": revision,
                    "receipt_revision": receipt_revision(p["id"]),"content_revision":content_revision(p["id"],user["name"])}
        with NEW_MSG:
            if (LAST_ID[0] == seen and reaction_revision(p["id"]) == revision
                    and receipt_revision(p["id"]) == receipts and content_revision(p["id"],user["name"]) == content):
                NEW_MSG.wait(remaining)


def attachment_name(value):
    name = str(value or "file").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if ord(c) >= 32 and c != chr(127))[:200]
    return name if name and name not in (".", "..") else "file"


def files_dir():
    path = os.path.abspath(Cfg.db) + ".files"
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def upload_path(uid):
    if not isinstance(uid, str) or not re.fullmatch(r"u_[0-9a-f]{48}", uid):
        raise ApiError(404, _ui_text('загрузка не найдена'))
    return os.path.join(files_dir(), uid)


def require_file_space(incoming=0):
    free = shutil.disk_usage(files_dir()).free
    if free < MIN_FREE_BYTES + incoming:
        raise ApiError(507, f"{_ui_text('Недостаточно места на сервере для файла: свободно ')}{free // (1024 * 1024)}{_ui_text(' МБ. Нужно сохранить не менее 300 МБ свободными. Можно отправить текст без вложения.')}", free_bytes=free, min_free_bytes=MIN_FREE_BYTES)


def own_upload(user, pid, uid):
    upload_path(uid)
    row = db().execute("SELECT * FROM file_uploads WHERE id=? AND user=? AND project_id=?",
                       (uid, user["name"], pid)).fetchone()
    if not row or not member_role(pid, user["name"]):
        raise ApiError(404, _ui_text('загрузка не найдена'))
    return row


def upload_status(row):
    return {"id": row["id"], "received": row["received"], "size": row["size"], "md5": row["md5"],
            "complete": row["md5"] is not None, "chunk_size": UPLOAD_CHUNK}


def discard_upload(uid):
    try:
        os.remove(upload_path(uid))
    except FileNotFoundError:
        pass
    db().execute("DELETE FROM file_uploads WHERE id=? AND attachment_id IS NULL", (uid,))


def scan_upload_chunk(uid, offset, raw, name):
    # Include the previous suffix so secrets split across requests cannot evade the filter.
    overlap = max([4096] + [len(x.encode("utf-8"))+512 for x in Cfg.deny_literals])
    with open(upload_path(uid), "rb") as f:
        f.seek(max(0, offset-overlap))
        tail = f.read(min(offset, overlap))
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text = decoder.decode(tail)
    for start in range(0, len(raw), 256*1024):
        part = raw[start:start+256*1024]
        text += decoder.decode(part, final=start+len(part) == len(raw))
        check_secret("файл " + name, text)
        text = text[-overlap:]


@route("GET", "/api/storage")
def api_storage(h, user, q, data):
    project_for(h, user, q)
    free = shutil.disk_usage(files_dir()).free
    return {"max_file_bytes": MAX_FILE_BYTES, "chunk_size": UPLOAD_CHUNK,
            "min_free_bytes": MIN_FREE_BYTES, "free_bytes": free, "can_upload": free >= MIN_FREE_BYTES}


@route("POST", "/api/uploads")
def api_upload_start(h, user, q, data):
    p = project_for(h, user, q)
    size, expected = data.get("size"), data.get("md5")
    if type(size) is not int or size < 0 or size > MAX_FILE_BYTES:
        raise ApiError(413, _ui_text('максимальный размер одного файла — 200 МБ'))
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{32}", expected)):
        raise ApiError(400, _ui_text('md5: ожидаются 32 шестнадцатеричных символа'))
    name = attachment_name(data.get("name"))
    check_secret("имя файла", name)
    key = str(data.get("key") or "")[:100] or None
    with UPLOAD_LOCK, DB_LOCK:
        # Abandoned drafts expire; adopted files and legacy BLOBs are never touched.
        for row in db().execute("SELECT id FROM file_uploads WHERE attachment_id IS NULL AND updated<?",
                                (time.time()-86400,)).fetchall():
            discard_upload(row["id"])
        if key:
            old = db().execute("SELECT * FROM file_uploads WHERE user=? AND project_id=? AND idem_key=?",
                               (user["name"], p["id"], key)).fetchone()
            if old:
                if (old["size"], old["name"], old["expected_md5"]) != (size, name, expected):
                    raise ApiError(409, _ui_text('ключ загрузки уже занят другим файлом'))
                return upload_status(old)
        require_file_space(size)
        uid, now = "u_"+secrets.token_hex(24), time.time()
        with open(upload_path(uid), "xb"):
            pass
        try:
            db().execute("INSERT INTO file_uploads(id,project_id,user,name,size,expected_md5,idem_key,created,updated) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (uid, p["id"], user["name"], name, size, expected, key, now, now))
        except Exception:
            os.remove(upload_path(uid))
            raise
        return upload_status(own_upload(user, p["id"], uid))


@route("POST", r"/api/uploads/(u_[0-9a-f]{48})/chunk", raw=True)
def api_upload_chunk(h, user, q, data, uid):
    p = project_for(h, user, q)
    try:
        size, offset = int(h.headers.get("Content-Length", "-1")), int(q.get("offset", "-1"))
    except ValueError:
        raise ApiError(400, _ui_text('нужны Content-Length и offset'))
    if size < 1 or size > UPLOAD_CHUNK or offset < 0 or h.headers.get("Transfer-Encoding"):
        raise ApiError(413, _ui_text('часть файла должна быть от 1 байта до 4 МБ'))
    with DB_LOCK:
        row = own_upload(user, p["id"], uid)
        if row["attachment_id"] or row["md5"] is not None:
            raise ApiError(409, _ui_text('загрузка уже завершена'))
        if offset > row["received"] or offset+size > row["size"]:
            raise ApiError(409, _ui_text('неверная позиция загрузки'), received=row["received"])
    h.connection.settimeout(90)
    raw = h.rfile.read(size)
    if len(raw) != size:
        raise ApiError(400, _ui_text('часть файла получена не полностью — повтори загрузку'))
    with UPLOAD_LOCK, DB_LOCK:
        row = own_upload(user, p["id"], uid)
        if row["attachment_id"] or row["md5"] is not None:
            raise ApiError(409, _ui_text('загрузка уже завершена'))
        if offset == row["received"]:
            try:
                scan_upload_chunk(uid, offset, raw, row["name"])
            except ApiError:
                discard_upload(uid)
                raise
        with open(upload_path(uid), "r+b") as f:
            if offset < row["received"]:
                f.seek(offset)
                if offset+size > row["received"] or f.read(size) != raw:
                    raise ApiError(409, _ui_text('повторная часть отличается от полученной'))
                return upload_status(row)
            if offset != row["received"]:
                raise ApiError(409, _ui_text('неверная позиция загрузки'), received=row["received"])
            require_file_space(size)
            f.truncate(offset); f.seek(offset)
            try:
                f.write(raw); f.flush(); os.fsync(f.fileno())
                db().execute("UPDATE file_uploads SET received=?,updated=? WHERE id=?", (offset+size, time.time(), uid))
            except OSError as error:
                f.truncate(offset)
                if error.errno == 28:
                    raise ApiError(507, _ui_text('На сервере закончилось место. Отправь текст без вложения.'))
                raise
        return upload_status(own_upload(user, p["id"], uid))


@route("POST", r"/api/uploads/(u_[0-9a-f]{48})/complete")
def api_upload_complete(h, user, q, data, uid):
    p = project_for(h, user, q)
    with UPLOAD_LOCK, DB_LOCK:
        row = own_upload(user, p["id"], uid)
        if row["md5"] is not None:
            return upload_status(row)
        if row["received"] != row["size"]:
            raise ApiError(409, _ui_text('файл получен не полностью'), received=row["received"])
        digest = hashlib.md5()
        try:
            with open(upload_path(uid), "rb") as f:
                while True:
                    chunk = f.read(256*1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            if row["expected_md5"] and digest.hexdigest() != row["expected_md5"]:
                raise ApiError(409, _ui_text('файл изменился во время отправки или повреждён; отправь его заново'))
        except ApiError:
            discard_upload(uid)
            raise
        db().execute("UPDATE file_uploads SET md5=?,updated=? WHERE id=?", (digest.hexdigest(), time.time(), uid))
        return upload_status(own_upload(user, p["id"], uid))


@route("POST", r"/api/uploads/(u_[0-9a-f]{48})/cancel")
def api_upload_cancel(h, user, q, data, uid):
    p = project_for(h, user, q)
    with UPLOAD_LOCK, DB_LOCK:
        row = own_upload(user, p["id"], uid)
        if row["attachment_id"]:
            raise ApiError(409, _ui_text('отправленный файл удаляется только вместе с проектом'))
        discard_upload(uid)
    return {"ok": True}


def attachment_for(user, aid):
    with DB_LOCK:
        a = db().execute("SELECT a.*, m.project_id FROM attachments a JOIN messages m ON m.id = a.message_id "
                         "WHERE a.id = ? AND m.deleted_ts IS NULL", (aid,)).fetchone()
        if a and not can_read_message(db().execute("SELECT * FROM messages WHERE id=?", (a["message_id"],)).fetchone(), user):
            a = None
    if not a:
        raise ApiError(404, _ui_text('нет такого вложения'))
    if a["content"] is None and a["upload_id"] is None:
        raise ApiError(409, f"{_ui_text('вложение по ссылке: ')}{a['link']}")
    return a


def stream_attachment(h, user, aid):
    a = attachment_for(user, aid)
    stream = open(upload_path(a["upload_id"]), "rb") if a["upload_id"] else io.BytesIO(a["content"])
    with stream:
        size = a["size"]
        start, end, code = 0, size-1, 200
        if h.headers.get("Range"):
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", h.headers["Range"])
            if not match:
                raise ApiError(416, _ui_text('некорректный диапазон файла'))
            start = int(match[1]); end = min(int(match[2]), end) if match[2] else end
            if start > end or start >= size:
                raise ApiError(416, _ui_text('диапазон выходит за размер файла'))
            code = 206
        h.send_response(code)
        for key, value in {"Content-Type": "application/octet-stream", "Content-Length": str(max(0, end-start+1)),
                           "Content-Disposition": "attachment; filename*=UTF-8''"+urllib.parse.quote(attachment_name(a["name"])),
                           "X-MD5": a["md5"] or "", "Accept-Ranges": "bytes", "Cache-Control": "no-store",
                           "X-Content-Type-Options": "nosniff"}.items():
            h.send_header(key, value)
        if code == 206:
            h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        h.end_headers(); stream.seek(start)
        remaining = end-start+1
        while remaining > 0:
            chunk = stream.read(min(256*1024, remaining))
            if not chunk:
                break
            h.wfile.write(chunk); remaining -= len(chunk)


@route("GET", r"/api/attachments/(\d+)")
def api_attachment(h, user, q, data, aid):
    stream_attachment(h, user, aid)


@route("POST", r"/api/attachments/(\d+)/download")
def api_download_ticket(h, user, q, data, aid):
    attachment_for(user, aid)
    ticket, now = secrets.token_urlsafe(32), time.time()
    with DB_LOCK:
        for key in list(DOWNLOADS):
            if DOWNLOADS[key][2] < now:
                del DOWNLOADS[key]
        if len(DOWNLOADS) >= 1024:
            DOWNLOADS.pop(next(iter(DOWNLOADS)))
        DOWNLOADS[token_hash(ticket)] = (aid, h.session_hash or user["token_hash"], now+300)
    path = f"/api/downloads/{aid}"
    cookie = f"tg_download_{aid}={ticket}; Path={path}; Max-Age=300; HttpOnly; SameSite=Strict"
    if h.headers.get("X-Forwarded-Proto") == "https":
        cookie += "; Secure"
    h.send_json(200, {"url": path}, {"Set-Cookie": cookie})


@route("GET", r"/api/downloads/(\d+)", auth=False)
def api_browser_download(h, user, q, data, aid):
    cookies = SimpleCookie()
    try:
        cookies.load(h.headers.get("Cookie", ""))
        value = cookies[f"tg_download_{aid}"].value
    except (KeyError, ValueError, CookieError):
        raise ApiError(401, _ui_text('нажми «Скачать» у файла в чате'))
    with DB_LOCK:
        ticket = DOWNLOADS.get(token_hash(value))
        if not ticket or ticket[0] != aid or ticket[2] < time.time():
            raise ApiError(401, _ui_text('ссылка на скачивание истекла; нажми «Скачать» ещё раз'))
        user = db().execute("SELECT u.* FROM users u WHERE u.revoked=0 AND (u.token_hash=? OR EXISTS "
                            "(SELECT 1 FROM sessions s WHERE s.user=u.name AND s.token_hash=?))",
                            (ticket[1], ticket[1])).fetchone()
        if not user:
            raise ApiError(401, _ui_text('вход больше не действителен'))
    stream_attachment(h, user, aid)


@route("GET", "/api/board")
def api_board(h, user, q, data):
    p = project_for(h, user, q)
    with DB_LOCK:
        rows = db().execute("SELECT * FROM board WHERE project_id = ? ORDER BY name", (p["id"],)).fetchall()
    return {"board": [{"id": r["name"], "name": account_name(r["name"]), "now": r["now"], "next": r["next"], "gpu_until": r["gpu_until"],
                       "note": r["note"], "back": r["back"], "time": iso(r["ts"]),
                       "age": round(time.time() - r["ts"]) if r["ts"] else None} for r in rows]}


@route("POST", "/api/board")
def api_board_set(h, user, q, data):
    p = project_for(h, user, q)
    vals = {}
    for k in ("now", "next", "gpu_until", "note", "back"):
        v = data.get(k)
        if v is not None:
            v = str(v)[:500]
            check_secret(k, v)
            vals[k] = v
    with DB_LOCK:
        con = db()
        con.execute("INSERT OR IGNORE INTO board(project_id, name, ts) VALUES (?, ?, ?)", (p["id"], user["name"], time.time()))
        for k, v in vals.items():
            con.execute(f"UPDATE board SET {k} = ? WHERE project_id = ? AND name = ?", (v or None, p["id"], user["name"]))
        con.execute("UPDATE board SET ts = ? WHERE project_id = ? AND name = ?", (time.time(), p["id"], user["name"]))
    return api_board(h, user, q, data)


@route("GET", "/api/claims")
def api_claims(h, user, q, data):
    p = project_for(h, user, q)
    with DB_LOCK:
        return {"claims": list_claims(q, p["id"])}


@route("POST", "/api/claims")
def api_claim(h, user, q, data):
    display = str(data.get("key") or "").strip()[:200]
    key = norm_key(display)
    if not key:
        raise ApiError(400, _ui_text('нужен key — что именно занимаешь'))
    check_secret("key", display)
    p = project_for(h, user, q)
    vals = {"topic": claim_text(data, "topic", 60), "note": claim_text(data, "note", 500),
            "until": claim_text(data, "until", 100)}
    try:
        hours = float(data["hours"]) if data.get("hours") is not None else CLAIM_HOURS
    except (TypeError, ValueError):
        raise ApiError(400, _ui_text('hours должно быть числом (часы, можно дробное)'))
    if not 0 < hours <= 24 * 365:
        raise ApiError(400, _ui_text('hours должно быть больше 0 и не больше года'))
    expires = time.time() + hours * 3600
    with DB_LOCK:
        con = db()
        cur = con.execute("SELECT * FROM claims WHERE key = ? AND released IS NULL AND project_id = ? ORDER BY id DESC",
                          (key, p["id"])).fetchone()
        taken = None
        if cur and cur["owner"] == user["name"]:      # same owner again: idempotent, extend the lifetime, refresh what was passed
            con.execute("UPDATE claims SET expires = ? WHERE id = ?", (expires, cur["id"]))
            for k, v in vals.items():
                if data.get(k) is not None:
                    con.execute(f"UPDATE claims SET {k} = ? WHERE id = ?", (v if k == "topic" else (v or None), cur["id"]))
            row = con.execute("SELECT * FROM claims WHERE id = ?", (cur["id"],)).fetchone()
            return {"claim": claim_dict(row), "created": False}
        if cur and cur["expires"] is not None and cur["expires"] < time.time():
            taken = release_claim(cur, "истекла")      # a lapsed claim is free for anyone, no --force needed
            res_expired, cur = taken, None
        else:
            res_expired = None
        if cur:
            if not data.get("force"):
                raise ApiError(409, f"«{cur['display_key']}{_ui_text('» уже занято: ')}{account_name(cur['owner'])}", holder=claim_dict(cur))
            taken = release_claim(cur, f"перехвачено {account_name(user['name'])}")
        cid = con.execute("INSERT INTO claims(key, display_key, topic, owner, owner_kind, host, note, until, created,"
                          " project_id, expires) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (key, display, vals["topic"], user["name"], user["kind"], h.host(), vals["note"] or None,
                           vals["until"] or None, time.time(), p["id"], expires)).lastrowid
        res = {"claim": claim_dict(con.execute("SELECT * FROM claims WHERE id = ?", (cid,)).fetchone()), "created": True}
        if res_expired:
            res["expired_released"] = res_expired
        elif taken:
            res["taken_over"] = taken
        return res


@route("POST", "/api/claims/release")
def api_claim_release(h, user, q, data):
    note = claim_text(data, "note", 500)
    p = project_for(h, user, q)
    with DB_LOCK:
        con = db()
        if data.get("id") is not None:
            try:
                row = con.execute("SELECT * FROM claims WHERE id = ? AND project_id = ?", (int(data["id"]), p["id"])).fetchone()
            except (TypeError, ValueError):
                raise ApiError(400, _ui_text('id должен быть числом'))
            if not row:
                raise ApiError(404, f"{_ui_text('заявки #')}{data['id']}{_ui_text(' нет')}")
            if row["released"] is not None:
                raise ApiError(409, f"{_ui_text('заявка #')}{row['id']} «{row['display_key']}{_ui_text('» уже снята')}")
        elif data.get("key"):
            rows = con.execute("SELECT * FROM claims WHERE key = ? AND released IS NULL AND project_id = ? ORDER BY id DESC",
                               (norm_key(data["key"]), p["id"])).fetchall()
            if not rows:
                raise ApiError(404, f"{_ui_text('активной заявки «')}{str(data['key'])[:80]}{_ui_text('» нет')}")
            row = next((r for r in rows if r["owner"] == user["name"]), rows[0])
        else:
            raise ApiError(400, _ui_text('нужен key или id'))
        if row["owner"] != user["name"]:
            if user["kind"] != "human":
                raise ApiError(403, f"«{row['display_key']}{_ui_text('» занял ')}{account_name(row['owner'])}{_ui_text(': чужую заявку снимает только человек')}")
            note = note or f"снято {account_name(user['name'])}"
        return {"claim": release_claim(row, note)}


# ---------------------------------------------------------------- CLI
def cmd_development_agent(a):
    """Operator binds one agent to the admin's private development chat; no secret in stdout."""
    if not NAME_RE.fullmatch(a.name):
        raise ApiError(400, _ui_text('недопустимое имя агента'))
    target = os.path.abspath(os.path.expanduser(a.token_out))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with DB_LOCK:
        con = db()
        admin = resolve_account(a.admin, kind="human")
        con.execute("BEGIN IMMEDIATE")
        try:
            project = development_project(con, admin)
            key = project["service_agent"]
            tok = None
            if key:
                agent = resolve_account(account_id=key)
                if agent["revoked"]:
                    raise ApiError(409, _ui_text('аккаунт сессии отозван; сначала реши это штатным управлением аккаунтами'))
                saved = None
                if os.path.isfile(target):
                    with open(target, encoding="utf-8") as f:
                        saved = f.read().strip()
                if saved and token_hash(saved) == agent["token_hash"] and not a.rotate:
                    pass
                elif not a.rotate:
                    raise ApiError(409, _ui_text('агент уже привязан; нужен его прежний файл токена или явный --rotate'))
                else:
                    tok = "tgt_" + secrets.token_hex(24)
                    con.execute("UPDATE users SET token_hash=? WHERE name=?", (token_hash(tok), key))
            else:
                if os.path.exists(target):
                    raise ApiError(409, _ui_text('файл токена уже существует: выбери другой путь'))
                tok = "tgt_" + secrets.token_hex(24)
                key = new_account(con, a.name, "agent", admin["name"], tok)
                con.execute("UPDATE projects SET service_agent=? WHERE id=?", (key, project["id"]))
                add_member(con, project["id"], key, "agent", admin["name"])
            if tok:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(tok + "\n")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    print(json.dumps({"project": project["slug"], "account_id": key, "name": account_name(key),
                      "token_file": target}, ensure_ascii=False))


def cmd_adduser(a):
    if not NAME_RE.fullmatch(a.name):
        sys.exit("имя: латиница/цифры/._- до 32 символов")
    tok = "tgt_" + secrets.token_hex(24)
    with DB_LOCK:
        con = db()
        pid = cli_project(con, a.project, create=not a.project)
        rows = find_accounts(a.name, pid if a.kind == "agent" else None, kind=a.kind)
        con.execute("BEGIN IMMEDIATE")
        try:
            if rows:
                if not a.rotate:
                    raise ApiError(409, _ui_text('имя уже есть в этой области; --rotate выдаст новый токен'))
                key = rows[0]["name"]
                con.execute("UPDATE users SET token_hash=?,revoked=0 WHERE name=?", (token_hash(tok), key))
            else:
                owner = resolve_account(a.owner, kind="human")["name"] if a.kind == "agent" else None
                key = new_account(con, a.name, a.kind, owner, tok)
                first = a.kind == "human" and not con.execute("SELECT 1 FROM members WHERE project_id=? AND role='owner'", (pid,)).fetchone()
                add_member(con, pid, key, "owner" if first else "member", "cli")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    if a.token_out:
        os.makedirs(os.path.dirname(os.path.abspath(a.token_out)), exist_ok=True)
        with open(a.token_out, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
        print(f"{a.name} ({a.kind}): токен записан в {a.token_out}")
    else:
        print(f"{a.name} ({a.kind}). Токен (показывается один раз):\n{tok}")


def cli_account(a, kind=None):
    pid = cli_project(db(), a.project) if getattr(a, "project", None) else None
    return resolve_account(a.name, pid, kind=kind, account_id=getattr(a, "account_id", None))


def cmd_users(a):
    sessions = {r[0]: r[1] for r in db().execute("SELECT user, COUNT(*) FROM sessions GROUP BY user")}
    for r in db().execute("SELECT * FROM users ORDER BY kind, name"):
        pw = ("админ, " if r["is_admin"] else "") + ("пароль задан" if r["pw_hash"] else "")
        print(f"{account_name(r['name']):<20} {r['kind']:<6} id={r['name']} owner={account_name(r['owner']):<10} {pw:<20} сессий: {sessions.get(r['name'], 0)}"
              f"{'  REVOKED' if r['revoked'] else ''}")


def cmd_revoke(a):
    key = cli_account(a)["name"]
    db().execute("UPDATE users SET revoked=1 WHERE name=?", (key,))
    db().execute("DELETE FROM sessions WHERE user=?", (key,))
    print(f"{a.name}: токен отозван, сессии закрыты")


def cmd_admin(a):
    for name in a.names:
        key = resolve_account(name, kind="human")["name"]
        db().execute("UPDATE users SET is_admin=? WHERE name=?", (0 if a.off else 1, key))
        print(f"{name}: {'больше не админ' if a.off else 'админ'}")


def cli_project(con, slug, create=False):
    slug = slug or Cfg.default_project["slug"]
    r = con.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
    if r:
        return r["id"]
    if not create:
        sys.exit(f"проекта {slug} нет; есть: " + ", ".join(x["slug"] for x in con.execute("SELECT slug FROM projects")))
    return create_project_row(con, slug, Cfg.default_project["title"] if slug == Cfg.default_project["slug"] else slug,
                              "cli", creator_unknown=True)


def cmd_projects(a):
    con = db()
    for p in con.execute("SELECT * FROM projects ORDER BY id"):
        ms = con.execute("SELECT user, role FROM members WHERE project_id = ? ORDER BY role, user", (p["id"],)).fetchall()
        print(f"{p['slug']:<20} «{p['title']}»{'  (архив)' if p['archived'] else ''}")
        print("    " + ", ".join(f"{account_name(m['user'])} ({m['role']})" for m in ms))


def cmd_addmember(a):
    con = db()
    pid = cli_project(con, a.project)
    target = resolve_account(a.name, account_id=a.account_id)
    add_member(con, pid, target["name"], a.role, "cli")
    print(f"{a.name} теперь в проекте {a.project}")


def cmd_invite(a):
    rows = find_accounts(a.name, account_id=a.account_id)
    if len(rows) > 1 and not a.project and not a.account_id:
        raise ApiError(409, _ui_text('имя неоднозначно: укажи --project или --account-id'))
    if rows and (a.reset or not a.project):
        inv = create_invite("cli", a.name, reset=True, account_id=rows[0]["name"])
    else:
        owner = resolve_account(a.owner, kind="human")["name"] if a.owner else None
        inv = create_invite("cli", a.name, a.kind, owner, cli_project(db(), a.project), account_id=a.account_id)
    print(f"{inv['name']} ({inv['kind']}, {inv['purpose']}), действует до {inv['expires']}")
    print((Cfg.launcher_url + "#invite=" + inv["code"]) if Cfg.launcher_url else f"код: {inv['code']} (добавь к адресу: /#invite=КОД)")


def cmd_clearpassword(a):
    key = cli_account(a, kind="human")["name"]
    db().execute("UPDATE users SET pw_hash=NULL WHERE name=?", (key,))
    n = db().execute("DELETE FROM sessions WHERE user=?", (key,)).rowcount
    print(f"{a.name}: пароль сброшен, закрыто сессий: {n}. Войти можно по ссылке с токеном и задать новый.")


def cmd_serve(a):
    db()
    def expire_secrets():
        while True:
            purge_expired_secrets()
            purge_deleted_files()
            time.sleep(60)
    threading.Thread(target=expire_secrets, daemon=True).start()
    if Cfg.telegram:
        threading.Thread(target=telegram_worker, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"telegent {VERSION}: http://{a.host}:{a.port}/  db={Cfg.db}  last_id={LAST_ID[0]}"
          f"  telegram={'on' if Cfg.telegram else 'off'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    p = argparse.ArgumentParser(description="telegent server")
    p.add_argument("--config", default=os.path.join(HERE, "server.config.json"))
    p.add_argument("--db", help="путь к SQLite (по умолчанию telegent.db рядом с server.py)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("development-agent", help="привязать агента к личному чату администратора с разработчиком")
    s.add_argument("--admin", required=True)
    s.add_argument("--name", default="telegent-dev")
    s.add_argument("--token-out", required=True)
    s.add_argument("--rotate", action="store_true")
    s.set_defaults(fn=cmd_development_agent)
    s = sub.add_parser("adduser")
    s.add_argument("name")
    s.add_argument("--kind", choices=("agent", "human"), required=True)
    s.add_argument("--owner", help="чей агент (имя человека); у человека — он сам")
    s.add_argument("--rotate", action="store_true")
    s.add_argument("--token-out", help="записать токен в файл, а не печатать")
    s.add_argument("--project", help="в какой проект (по умолчанию — проект по умолчанию, он создаётся сам)")
    s.set_defaults(fn=cmd_adduser)
    s = sub.add_parser("projects", help="проекты и их участники")
    s.set_defaults(fn=cmd_projects)
    s = sub.add_parser("addmember", help="добавить существующий аккаунт в проект")
    s.add_argument("project")
    s.add_argument("name")
    s.add_argument("--role", choices=("owner", "member"), default="member")
    s.add_argument("--account-id")
    s.set_defaults(fn=cmd_addmember)
    s = sub.add_parser("users")
    s.set_defaults(fn=cmd_users)
    s = sub.add_parser("revoke")
    s.add_argument("name")
    s.add_argument("--project")
    s.add_argument("--account-id")
    s.set_defaults(fn=cmd_revoke)
    s = sub.add_parser("clearpassword", help="сбросить пароль человека и закрыть его сессии")
    s.add_argument("name")
    s.add_argument("--account-id")
    s.set_defaults(fn=cmd_clearpassword)
    s = sub.add_parser("admin", help="сделать людей админами (приглашения, сброс паролей)")
    s.add_argument("names", nargs="+")
    s.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_admin)
    s = sub.add_parser("invite", help="ссылка-приглашение: новый в проект, существующий в проект (--project), "
                                      "или сброс пароля / токена (существующее имя без --project)")
    s.add_argument("name")
    s.add_argument("--kind", choices=("human", "agent"))
    s.add_argument("--owner", help="владелец агента (человек)")
    s.add_argument("--project", help="проект (для новых — по умолчанию проект по умолчанию)")
    s.add_argument("--account-id")
    s.add_argument("--reset", action="store_true")
    s.set_defaults(fn=cmd_invite)
    a = p.parse_args()
    load_config(a.config)
    if a.db:
        Cfg.db = a.db
    try:
        a.fn(a)
    except (ApiError, sqlite3.IntegrityError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
