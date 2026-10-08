#!/usr/bin/env python3
"""telegent server: a small message bus for a team's agents and humans.

Stdlib only (Python 3.8+): HTTP + SQLite. One process, one file of data.

    python server.py adduser alice-agent --kind agent --owner alice  # prints the token once
    python server.py serve --port 8765                              # http://127.0.0.1:8765/

See README.md for hosting (tunnel) and AGENTS.md for the agent protocol.
"""
import argparse
import base64
import hashlib
import json
import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.0"
HERE = os.path.dirname(os.path.abspath(__file__))

USER_TYPES = ("info", "request", "question", "result", "alert", "decision")
SYSTEM_TYPES = ("status", "approval")
TRACKED_TYPES = ("request", "alert", "decision")      # these carry open/taken/done/rejected
STATES = ("open", "taken", "done", "rejected")
PRIORITIES = ("normal", "urgent")
RESULT_FIELDS = ("metric", "dataset", "value", "err", "baseline", "commit", "weights", "note")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")

MAX_REQUEST = 40 * 1024 * 1024
MAX_BODY_CHARS = 200_000

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
  result TEXT, idem_key TEXT,
  UNIQUE(author, idem_key));
CREATE INDEX IF NOT EXISTS ix_msg_thread ON messages(thread_id);
CREATE TABLE IF NOT EXISTS approvals(
  message_id INTEGER NOT NULL, user TEXT NOT NULL, decision TEXT NOT NULL, note TEXT, ts REAL NOT NULL,
  PRIMARY KEY(message_id, user));
CREATE TABLE IF NOT EXISTS attachments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, name TEXT NOT NULL,
  size INTEGER, md5 TEXT, link TEXT, content BLOB);
CREATE INDEX IF NOT EXISTS ix_att_msg ON attachments(message_id);
CREATE TABLE IF NOT EXISTS receipts(
  user TEXT NOT NULL, message_id INTEGER NOT NULL, read_ts REAL, ack_ts REAL,
  PRIMARY KEY(user, message_id));
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
NEW_MSG = threading.Condition()
LAST_ID = [0]
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
                                 ("users", "last_wait_ts", "REAL"), ("users", "pw_hash", "TEXT"),
                                 ("users", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
                                 ("messages", "project_id", "INTEGER"), ("claims", "project_id", "INTEGER"),
                                 ("invites", "project_id", "INTEGER"), ("invites", "role", "TEXT")):
            if col not in {r[1] for r in _db.execute(f"PRAGMA table_info({table})")}:
                _db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        _db.executescript("CREATE INDEX IF NOT EXISTS ix_msg_project ON messages(project_id, id);")
        if "project_id" not in {r[1] for r in _db.execute("PRAGMA table_info(board)")}:
            # the board used to be one per server: rebuild it keyed by (project, name)
            _db.executescript("""
                ALTER TABLE board RENAME TO board_old;
                CREATE TABLE board(project_id INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL, now TEXT, next TEXT,
                  gpu_until TEXT, note TEXT, ts REAL, PRIMARY KEY(project_id, name));
                INSERT INTO board(project_id, name, now, next, gpu_until, note, ts)
                  SELECT 0, name, now, next, gpu_until, note, ts FROM board_old;
                DROP TABLE board_old;""")
        _db.create_function("pylower", 1, lambda s: s.lower() if isinstance(s, str) else s)
        ensure_default_project(_db)
        LAST_ID[0] = _db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
    return _db


# ---------------------------------------------------------------- projects
# A project is one team's chat: its own feed, board, claims and results, and its own members.
# People (owner | member) may be in many projects; an agent belongs to exactly one.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


def slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")[:40]
    return s if SLUG_RE.match(s) else "project"


def create_project_row(con, slug, title, by):
    base, n = slug, 2
    while con.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base[:36]}-{n}"
        n += 1
    return con.execute("INSERT INTO projects(slug, title, created_by, created_ts) VALUES (?,?,?,?)",
                       (slug, title[:80], by, time.time())).lastrowid


def add_member(con, pid, name, role, by):
    u = con.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
    if u["kind"] == "agent":
        role = "agent"
        other = con.execute("SELECT p.slug FROM members m JOIN projects p ON p.id = m.project_id "
                            "WHERE m.user = ? AND m.project_id != ?", (name, pid)).fetchone()
        if other:
            raise ApiError(409, f"агент {name} уже в проекте «{other['slug']}», а агент бывает только в одном проекте")
    con.execute("INSERT OR REPLACE INTO members(project_id, user, role, added_by, added_ts) VALUES (?,?,?,?,?)",
                (pid, name, role, by, time.time()))


def ensure_default_project(con):
    """A database from before projects: everything moves into one project, with all users as members."""
    if con.execute("SELECT 1 FROM projects LIMIT 1").fetchone() or not con.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return
    pid = create_project_row(con, Cfg.default_project["slug"], Cfg.default_project["title"], "migration")
    users = con.execute("SELECT * FROM users WHERE revoked = 0 ORDER BY created_ts").fetchall()
    owners = [u["name"] for u in users if u["kind"] == "human" and u["is_admin"]] or \
             [u["name"] for u in users if u["kind"] == "human"][:1]
    for u in users:
        add_member(con, pid, u["name"], "owner" if u["name"] in owners else "member", "migration")
    for table in ("messages", "claims"):
        con.execute(f"UPDATE {table} SET project_id = ? WHERE project_id IS NULL", (pid,))
    con.execute("UPDATE board SET project_id = ? WHERE project_id = 0", (pid,))
    con.execute("UPDATE invites SET project_id = ? WHERE project_id IS NULL AND purpose = 'new'", (pid,))


def my_projects(name):
    return db().execute("SELECT p.*, m.role FROM members m JOIN projects p ON p.id = m.project_id "
                        "WHERE m.user = ? AND p.archived = 0 ORDER BY p.id", (name,)).fetchall()


def member_role(pid, name):
    r = db().execute("SELECT role FROM members WHERE project_id = ? AND user = ?", (pid, name)).fetchone()
    return r["role"] if r else None


def project_for(h, user, q):
    """The project a request is about: ?project= / X-Telegent-Project, or the only one the user is in."""
    slug = str(q.get("project") or h.headers.get("X-Telegent-Project") or "").strip()
    with DB_LOCK:
        rows = my_projects(user["name"])
    if slug:
        p = next((r for r in rows if r["slug"] == slug), None)
        if not p:
            raise ApiError(404, f"проекта «{slug}» нет или ты в нём не состоишь")
        return p
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise ApiError(409, "ты пока не состоишь ни в одном проекте: создай проект или попроси приглашение")
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
            raise ApiError(429, f"много неверных попыток, подожди {human_wait(lock - (now - fails[-1]))} и попробуй ещё. "
                                f"Забыл пароль — попроси ссылку для сброса")


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


def create_invite(by, name, kind=None, owner=None, pid=None, reset=False):
    if not NAME_RE.match(name or ""):
        raise ApiError(400, "имя: латиница, цифры, . _ -, до 32 символов, например masha или masha-agent")
    with DB_LOCK:
        con = db()
        u = con.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
        if u and u["revoked"]:
            raise ApiError(400, f"{name} отозван; заведи новое имя")
        role = None
        if reset:
            if not u:
                raise ApiError(404, f"{name} нет")
            kind, owner, purpose, pid = u["kind"], u["owner"], "reset", None
        elif u:
            kind, owner, purpose = u["kind"], u["owner"], "add"
            if member_role(pid, name):
                raise ApiError(409, f"{name} уже в этом проекте")
            if kind == "agent":
                other = con.execute("SELECT p.slug FROM members m JOIN projects p ON p.id = m.project_id WHERE m.user = ?",
                                    (name,)).fetchone()
                if other:
                    raise ApiError(409, f"агент {name} уже в проекте «{other['slug']}», а агент бывает только в одном")
            role = "agent" if kind == "agent" else "member"
        else:
            kind = kind or "human"
            if kind not in ("human", "agent"):
                raise ApiError(400, "kind: human | agent")
            if kind == "human":
                owner, role = name, "member"
            else:
                owner, role = owner or by, "agent"
                o = con.execute("SELECT kind FROM users WHERE name = ? AND revoked = 0", (owner,)).fetchone()
                if not o or o["kind"] != "human" or not member_role(pid, owner):
                    raise ApiError(400, f"владелец агента должен быть человеком из этого проекта, а не «{owner}»")
            purpose = "new"
        code = "tgi_" + secrets.token_hex(24)
        now = time.time()
        # one live link per name and purpose (per project)
        con.execute("DELETE FROM invites WHERE name = ? AND used_ts IS NULL AND purpose = ? AND "
                    "COALESCE(project_id, 0) = COALESCE(?, 0)", (name, purpose, pid))
        con.execute("INSERT INTO invites(code_hash, name, kind, owner, purpose, created_by, created_ts, expires_ts,"
                    " project_id, role) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (token_hash(code), name, kind, owner, purpose, by, now, now + INVITE_DAYS * 86400, pid, role))
    return {"code": code, "name": name, "kind": kind, "owner": owner, "purpose": purpose,
            "expires": iso(now + INVITE_DAYS * 86400)}


def live_invite(code):
    with DB_LOCK:
        inv = db().execute("SELECT i.*, p.slug AS project_slug, p.title AS project_title FROM invites i "
                           "LEFT JOIN projects p ON p.id = i.project_id WHERE i.code_hash = ?",
                           (token_hash(code or ""),)).fetchone()
    if not inv:
        raise ApiError(404, "приглашение не найдено: ссылка неверная или её заменили новой")
    if inv["used_ts"]:
        raise ApiError(410, "приглашение уже использовано; если это был не ты, скажи тому, кто приглашал")
    if inv["expires_ts"] < time.time():
        raise ApiError(410, "приглашение просрочено; попроси новое")
    return inv


def redeem_invite(code, password=None, label="", me=None):
    """new: a session (person) or a token (agent); add: joins the project (me must be that account); reset: as new."""
    inv = live_invite(code)
    name, kind, purpose = inv["name"], inv["kind"], inv["purpose"]
    if purpose == "add":
        if not me or me["name"] != name:
            raise ApiError(401, f"это приглашение в проект для {name}: войди как {name} и открой ссылку снова")
    elif kind == "human" and len(password or "") < PW_MIN_LEN:
        raise ApiError(400, f"придумай пароль от {PW_MIN_LEN} символов")
    tok = "tgt_" + secrets.token_hex(24)
    with DB_LOCK:
        con = db()
        con.execute("BEGIN IMMEDIATE")
        try:
            # re-check under the lock: a link works once
            if con.execute("SELECT used_ts FROM invites WHERE code_hash = ?", (inv["code_hash"],)).fetchone()["used_ts"]:
                raise ApiError(410, "приглашение уже использовано")
            con.execute("UPDATE invites SET used_ts = ? WHERE code_hash = ?", (time.time(), inv["code_hash"]))
            pw = hash_password(password) if kind == "human" and purpose != "add" else None
            if purpose == "new":
                if con.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
                    raise ApiError(409, f"{name} уже есть")
                con.execute("INSERT INTO users(name, kind, owner, token_hash, created_ts, pw_hash) VALUES (?,?,?,?,?,?)",
                            (name, kind, inv["owner"], token_hash(tok), time.time(), pw))
                add_member(con, inv["project_id"], name, inv["role"], inv["created_by"])
            elif purpose == "add":
                add_member(con, inv["project_id"], name, inv["role"], inv["created_by"])
            elif kind == "human":
                con.execute("UPDATE users SET pw_hash = ? WHERE name = ?", (pw, name))
                con.execute("DELETE FROM sessions WHERE user = ?", (name,))
                _login_fails.pop(f"name:{name.lower()}", None)
            else:
                con.execute("UPDATE users SET token_hash = ? WHERE name = ?", (token_hash(tok), name))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    res = {"name": name, "kind": kind, "purpose": purpose, "project": inv["project_slug"]}
    if purpose == "add":
        return res
    res["token"] = new_session(name, label) if kind == "human" else tok
    return res


class ApiError(Exception):
    def __init__(self, code, msg, **extra):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.extra = extra


def check_secret(field, text):
    hit = find_secret(text, Cfg.deny_literals)
    if hit:
        raise ApiError(422, f"похоже на секрет ({hit[0]}) в поле «{field}», строка {hit[1]}. "
                            f"Сообщение НЕ отправлено. Убери значение (напиши «см. файл X у меня») и пошли снова.")


# ---------------------------------------------------------------- presence
# A client "listens" while it keeps a /api/wait long-poll going (tg.py watch, the web page).
LISTEN_WINDOW = 75
_touched = {}


def touch(user, waiting=False):
    now = time.time()
    key = (user["name"], waiting)
    if now - _touched.get(key, 0) < 15:
        return
    _touched[key] = now
    with DB_LOCK:
        if waiting:
            db().execute("UPDATE users SET last_seen_ts = ?, last_wait_ts = ? WHERE name = ?", (now, now, user["name"]))
        else:
            db().execute("UPDATE users SET last_seen_ts = ? WHERE name = ?", (now, user["name"]))


def presence(row, now=None):
    now = now or time.time()
    lw = row["last_wait_ts"]
    return {"listening": bool(lw and now - lw < LISTEN_WINDOW), "last_listen": iso(lw), "last_seen": iso(row["last_seen_ts"])}


def users_with_presence(pid):
    """Members of a project (only them: other projects' people stay invisible) with presence and role."""
    now = time.time()
    return [dict(name=r["name"], kind=r["kind"], owner=r["owner"], role=r["role"], **presence(r, now))
            for r in db().execute("SELECT u.*, m.role FROM members m JOIN users u ON u.name = m.user "
                                  "WHERE m.project_id = ? AND u.revoked = 0 ORDER BY u.kind, u.name", (pid,))]


# ---------------------------------------------------------------- message helpers
def recipients_list(s):
    return [x for x in (s or "").split(",") if x]


def project_agents(pid):
    return [r["user"] for r in db().execute("SELECT m.user FROM members m JOIN users u ON u.name = m.user "
                                            "WHERE m.project_id = ? AND m.role = 'agent' AND u.revoked = 0 "
                                            "ORDER BY m.user", (pid,))]


def expected_recipients(pid, author, to):
    """Who should get a message: the addressees, or every agent of the project except the author."""
    if to:
        return [x for x in to if x != author]
    return [a for a in project_agents(pid) if a != author]


def msg_dicts(rows, me=None, full=True):
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    qs = ",".join("?" * len(ids))
    con = db()
    atts, receipts, approvals = {}, {}, {}
    for a in con.execute(f"SELECT id, message_id, name, size, md5, link, content IS NOT NULL AS inl "
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
            {"by": a["user"], "decision": a["decision"], "note": a["note"], "time": iso(a["ts"])})
    out = []
    for r in rows:
        rc = receipts.get(r["id"], [])
        mine = next((x for x in rc if x["user"] == me), None)
        d = {
            "id": r["id"], "time": iso(r["ts"]), "ts": r["ts"],
            "author": r["author"], "author_kind": r["author_kind"], "owner": r["owner"], "host": r["host"],
            "to": recipients_list(r["recipients"]), "topic": r["topic"], "type": r["type"],
            "priority": r["priority"], "subject": r["subject"],
            "body": r["body"] if full else r["body"][:400],
            "reply_to": r["reply_to"], "thread_id": r["thread_id"],
            "needs_approval": bool(r["needs_approval"]),
            "approvals": approvals.get(r["id"], []),
            "status": None if r["status"] is None else {
                "state": r["status"], "by": r["status_by"], "time": iso(r["status_ts"]),
                "ref": r["status_ref"], "note": r["status_note"]},
            "result": json.loads(r["result"]) if r["result"] else None,
            "attachments": atts.get(r["id"], []),
            "read": bool(mine and mine["read_ts"]), "acked": bool(mine and mine["ack_ts"]),
            "acked_by": [x["user"] for x in rc if x["ack_ts"]],
        }
        # per-recipient delivery: delivered (their watch/inbox got it) -> read -> acked
        by_user = {x["user"]: x for x in rc if x["user"] != r["author"]}
        to = d["to"] or [a for a in agents[r["project_id"]] if a != r["author"]]
        d["receipts"] = [{"user": u,
                          "delivered": iso(by_user[u]["delivered_ts"] or by_user[u]["read_ts"]) if u in by_user else None,
                          "read": iso(by_user[u]["read_ts"]) if u in by_user else None,
                          "acked": iso(by_user[u]["ack_ts"]) if u in by_user else None}
                         for u in to + [u for u in by_user if u not in to]]
        out.append(d)
    return out


def insert_message(user, host, data, pid, system=False):
    """Validate and store a message in project pid. Returns (id, duplicate)."""
    con = db()
    mtype = data.get("type") or "info"
    allowed = USER_TYPES + (SYSTEM_TYPES if system else ())
    if mtype not in allowed:
        raise ApiError(400, f"type должен быть одним из: {', '.join(USER_TYPES)}")
    priority = data.get("priority") or "normal"
    if priority not in PRIORITIES:
        raise ApiError(400, "priority: normal | urgent")
    body = str(data.get("body") or "")
    subject = str(data.get("subject") or "").strip()
    if len(body) > MAX_BODY_CHARS:
        raise ApiError(413, f"текст длиннее {MAX_BODY_CHARS} символов — положи его во вложение")
    if not subject:
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        subject = first[:120]
    if not subject and not data.get("attachments"):
        raise ApiError(400, "пустое сообщение")
    subject = subject.replace("\n", " ")[:200]
    topic = str(data.get("topic") or "").strip()[:60]

    to = data.get("to") or []
    if isinstance(to, str):
        to = [x.strip() for x in to.split(",")]
    to = [x for x in to if x and x != "all"]
    if to:
        known = {u["name"] for u in users_with_presence(pid)}
        bad = [x for x in to if x not in known]
        if bad:
            raise ApiError(400, f"нет таких адресатов в проекте: {', '.join(bad)}; есть: {', '.join(sorted(known))}")

    reply_to = data.get("reply_to")
    thread_id = None
    if reply_to is not None:
        parent = con.execute("SELECT * FROM messages WHERE id = ? AND project_id = ?", (int(reply_to), pid)).fetchone()
        if not parent:
            raise ApiError(404, f"сообщения #{reply_to} нет")
        reply_to = parent["id"]
        thread_id = parent["thread_id"]
        if not topic:
            topic = parent["topic"]

    result = data.get("result") or None
    if result is not None:
        if not isinstance(result, dict):
            raise ApiError(400, "result должен быть объектом")
        result = {k: v for k, v in result.items() if k in RESULT_FIELDS and v not in (None, "")}
        for k, v in result.items():
            if not isinstance(v, (str, int, float)) or len(str(v)) > 500:
                raise ApiError(400, f"result.{k}: строка или число до 500 символов")
        result = result or None

    needs_approval = bool(data.get("needs_approval")) or mtype == "decision"
    status = "open" if mtype in TRACKED_TYPES else None

    # attachments: decode + validate before writing anything
    atts = []
    for a in data.get("attachments") or []:
        name = os.path.basename(str(a.get("name") or "file"))[:200]
        check_secret("имя вложения", name)
        if a.get("content_b64") is not None:
            try:
                raw = base64.b64decode(a["content_b64"], validate=True)
            except Exception:
                raise ApiError(400, f"вложение {name}: битый base64")
            if len(raw) > Cfg.max_inline:
                raise ApiError(413, f"вложение {name} больше {Cfg.max_inline} байт — пришли ссылкой и md5")
            check_secret(f"вложение {name}", raw.decode("utf-8", errors="replace"))
            atts.append((name, len(raw), hashlib.md5(raw).hexdigest(), None, raw))
        elif a.get("link"):
            link = str(a["link"])[:1000]
            check_secret(f"ссылка {name}", link)
            atts.append((name, a.get("size"), (a.get("md5") or None), link, None))
        else:
            raise ApiError(400, f"вложение {name}: нужен content_b64 или link")
    if len(atts) > 30:
        raise ApiError(400, "не больше 30 вложений")

    check_secret("тема", subject)
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
                " subject, body, reply_to, thread_id, needs_approval, status, result, idem_key, project_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), user["name"], user["kind"], user["owner"], (host or "")[:120],
                 ("," + ",".join(to) + ",") if to else "", topic, mtype, priority, subject, body,
                 reply_to, thread_id, int(needs_approval), status,
                 json.dumps(result, ensure_ascii=False) if result else None, idem, pid))
            mid = cur.lastrowid
            if thread_id is None:
                con.execute("UPDATE messages SET thread_id = ? WHERE id = ?", (mid, mid))
            for name, size, md5, link, raw in atts:
                con.execute("INSERT INTO attachments(message_id, name, size, md5, link, content) VALUES (?,?,?,?,?,?)",
                            (mid, name, size, md5, link, raw))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    with NEW_MSG:
        LAST_ID[0] = max(LAST_ID[0], mid)
        NEW_MSG.notify_all()
    if Cfg.telegram:
        TG_QUEUE.put(mid)
    return mid, False


def get_message_row(mid, user=None):
    """A message by id; with user, only if the user is in its project (otherwise it "does not exist")."""
    r = db().execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    if not r or (user is not None and not member_role(r["project_id"], user["name"])):
        raise ApiError(404, f"сообщения #{mid} нет")
    return r


def list_messages(me, q, pid):
    where, args = ["m.project_id = ?"], [pid]
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
    if q.get("author"):
        where.append("m.author = ?")
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
    if q.get("pending_approval") == "1":
        where.append("m.needs_approval = 1 AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.message_id = m.id)")
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
            con.execute("INSERT OR IGNORE INTO receipts(user, message_id) VALUES (?, ?)", (me["name"], mid))
            con.execute("UPDATE receipts SET delivered_ts = COALESCE(delivered_ts, ?) WHERE user = ? AND message_id = ?",
                        (now, me["name"], mid))
            if kind in ("read", "ack"):
                con.execute("UPDATE receipts SET read_ts = COALESCE(read_ts, ?) WHERE user = ? AND message_id = ?",
                            (now, me["name"], mid))
            if kind == "ack":
                con.execute("UPDATE receipts SET ack_ts = COALESCE(ack_ts, ?) WHERE user = ? AND message_id = ?",
                            (now, me["name"], mid))


def set_status(me, host, mid, data):
    state = data.get("state")
    if state not in STATES:
        raise ApiError(400, f"state: {' | '.join(STATES)}")
    ref = str(data.get("ref") or "")[:1000]
    note = str(data.get("note") or "")[:5000]
    check_secret("ref", ref)
    check_secret("note", note)
    m = get_message_row(mid, me)
    if m["type"] not in TRACKED_TYPES:
        raise ApiError(400, f"статус есть только у {', '.join(TRACKED_TYPES)}; #{mid} — {m['type']}")
    with DB_LOCK:
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
        raise ApiError(403, "одобрять может только человек")
    decision = data.get("decision")
    if decision not in ("approve", "deny"):
        raise ApiError(400, "decision: approve | deny")
    note = str(data.get("note") or "")[:5000]
    check_secret("note", note)
    m = get_message_row(mid, me)
    if not m["needs_approval"]:
        raise ApiError(400, f"#{mid} не просит одобрения")
    now = time.time()
    with DB_LOCK:
        db().execute("INSERT OR REPLACE INTO approvals(message_id, user, decision, note, ts) VALUES (?,?,?,?,?)",
                     (mid, me["name"], decision, note or None, now))
        if m["type"] == "decision":
            db().execute("UPDATE messages SET status = ?, status_by = ?, status_ts = ? WHERE id = ?",
                         ("done" if decision == "approve" else "rejected", me["name"], now, mid))
    word = "ОДОБРЕНО" if decision == "approve" else "ОТКАЗАНО"
    return insert_message(me, host, {
        "type": "approval", "reply_to": mid, "subject": f"#{mid} {word} ({me['name']}): {m['subject'][:80]}",
        "body": note, "to": [], "priority": "urgent" if m["priority"] == "urgent" else "normal"}, m["project_id"], system=True)


# ---------------------------------------------------------------- experiment claims
def norm_key(s):
    return " ".join(str(s or "").lower().split())


def claim_dict(r):
    return {"id": r["id"], "key": r["display_key"], "norm_key": r["key"], "topic": r["topic"], "owner": r["owner"],
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
    if q.get("owner"):
        where.append("owner = ?")
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


def route(method, pattern, auth=True):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, auth, fn))
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
        parsed = urllib.parse.urlsplit(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            for m, rx, auth, fn in ROUTES:
                mt = rx.match(parsed.path)
                if m == method and mt:
                    user = self.auth() if auth else None
                    data = self.read_json() if method == "POST" else None
                    with_args = [int(x) if x.isdigit() else x for x in mt.groups()]
                    res = fn(self, user, q, data, *with_args)
                    if res is not None:
                        self.send_json(200, res)
                    return
            raise ApiError(404, "нет такого пути")
        except ApiError as e:
            self.send_json(e.code, dict(e.extra, error=e.msg))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.send_json(500, {"error": f"server error: {type(e).__name__}: {e}"})

    def auth(self):
        h = self.headers.get("Authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        if not tok:
            raise ApiError(401, "нужен токен (Authorization: Bearer ...)")
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
            raise ApiError(401, "неизвестный или отозванный токен (или сессия закрыта)")
        touch(u)
        return u

    def client_ip(self):
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def host(self):
        return self.headers.get("X-Telegent-Host", "")[:120] or "web"

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_REQUEST:
            raise ApiError(413, "запрос слишком большой")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            raise ApiError(400, "тело запроса — не JSON")

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_bytes(code, body, "application/json; charset=utf-8")

    def send_bytes(self, code, body, ctype, extra=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
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


@route("GET", "/api/info", auth=False)
def api_info(h, user, q, data):
    return {"version": VERSION, "server_time": iso(time.time()), "types": USER_TYPES, "states": STATES,
            "result_fields": RESULT_FIELDS, "max_inline_bytes": Cfg.max_inline, "launcher_url": Cfg.launcher_url}


def project_dict(p):
    return {"slug": p["slug"], "title": p["title"], "role": p["role"]}


@route("GET", "/api/me")
def api_me(h, user, q, data):
    """Who I am, my projects, and (if the request names one, or I am in exactly one) that project's members."""
    with DB_LOCK:
        projects = [project_dict(p) for p in my_projects(user["name"])]
        try:
            p = project_for(h, user, q)
        except ApiError:
            p = None
        users = users_with_presence(p["id"]) if p else []
        mine = presence(user)
    return {"name": user["name"], "kind": user["kind"], "owner": user["owner"],
            "project": project_dict(p) if p else None, "projects": projects, "users": users,
            "listening": mine["listening"], "last_listen": mine["last_listen"],
            "has_password": bool(user["pw_hash"]), "session": bool(h.session_hash), "is_admin": bool(user["is_admin"]),
            "server_time": iso(time.time()), "last_id": LAST_ID[0]}


@route("POST", "/api/login", auth=False)
def api_login(h, user, q, data):
    name = str(data.get("name") or "").strip()
    pw = str(data.get("password") or "")
    keys = [f"name:{name.lower()}", f"ip:{h.client_ip()}"]
    login_throttle(keys)
    with DB_LOCK:
        u = db().execute("SELECT * FROM users WHERE name = ? AND revoked = 0", (name,)).fetchone()
    if u and u["kind"] != "human":
        raise ApiError(403, "агенты входят по токену из файла, не по паролю")
    if u and not u["pw_hash"]:
        raise ApiError(400, f"у {name} ещё нет пароля: войди один раз по ссылке с токеном и задай его (кнопка «Пароль»)")
    if not u or not check_password(pw, u["pw_hash"]):
        login_failed(keys)
        raise ApiError(401, "неверное имя или пароль")
    _login_fails.pop(keys[0], None)
    return {"token": new_session(u["name"], h.headers.get("User-Agent", "")), "name": u["name"]}


@route("POST", "/api/logout")
def api_logout(h, user, q, data):
    if h.session_hash:
        with DB_LOCK:
            db().execute("DELETE FROM sessions WHERE token_hash = ?", (h.session_hash,))
    return {"ok": True, "session_closed": bool(h.session_hash)}


@route("POST", "/api/password")
def api_password(h, user, q, data):
    if user["kind"] != "human":
        raise ApiError(403, "пароль бывает только у людей; агенты входят по токену")
    new = str(data.get("new") or "")
    if len(new) < PW_MIN_LEN:
        raise ApiError(400, f"пароль короче {PW_MIN_LEN} символов")
    if user["pw_hash"]:
        keys = [f"name:{user['name'].lower()}", f"ip:{h.client_ip()}"]
        login_throttle(keys)
        if not check_password(str(data.get("old") or ""), user["pw_hash"]):
            login_failed(keys)
            raise ApiError(403, "текущий пароль неверный")
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
        raise ApiError(403, f"это может только владелец проекта «{p['slug']}»")


@route("POST", "/api/invites")
def api_invite_create(h, user, q, data):
    """Owner of a project: a new person / agent, or an existing account, into this project."""
    p = project_for(h, user, q)
    require_owner(p)
    inv = create_invite(user["name"], str(data.get("name") or "").strip(), data.get("kind"), data.get("owner"), p["id"])
    inv["link"] = invite_link(h, inv["code"])
    inv["project"] = p["slug"]
    return inv


@route("POST", "/api/reset")
def api_reset(h, user, q, data):
    """A new password / token by a one-time link: the server admin for anyone, a person for their own agents."""
    name = str(data.get("name") or "").strip()
    with DB_LOCK:
        u = db().execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
    if not u:
        raise ApiError(404, f"{name} нет")
    if not (user["is_admin"] or (u["kind"] == "agent" and u["owner"] == user["name"])):
        raise ApiError(403, "сбросить можно токен своего агента; пароль человека сбрасывает админ сервера")
    inv = create_invite(user["name"], name, reset=True)
    inv["link"] = invite_link(h, inv["code"])
    return inv


@route("GET", "/api/invites")
def api_invite_list(h, user, q, data):
    p = project_for(h, user, q)
    require_owner(p)
    with DB_LOCK:
        rows = db().execute("SELECT * FROM invites WHERE project_id = ? AND used_ts IS NULL AND expires_ts > ? "
                            "ORDER BY created_ts DESC", (p["id"], time.time())).fetchall()
    return {"invites": [{"name": r["name"], "kind": r["kind"], "purpose": r["purpose"], "by": r["created_by"],
                         "expires": iso(r["expires_ts"])} for r in rows]}


@route("POST", "/api/invites/cancel")
def api_invite_cancel(h, user, q, data):
    p = project_for(h, user, q)
    require_owner(p)
    with DB_LOCK:
        n = db().execute("DELETE FROM invites WHERE project_id = ? AND name = ? AND used_ts IS NULL",
                         (p["id"], str(data.get("name") or ""))).rowcount
    return {"ok": True, "cancelled": n}


@route("GET", "/api/invite", auth=False)
def api_invite_info(h, user, q, data):
    inv = live_invite(q.get("code"))
    return {"name": inv["name"], "kind": inv["kind"], "owner": inv["owner"], "purpose": inv["purpose"],
            "project": inv["project_slug"], "project_title": inv["project_title"], "expires": iso(inv["expires_ts"])}


@route("POST", "/api/join", auth=False)
def api_join(h, user, q, data):
    me = None
    if h.headers.get("Authorization"):
        me = h.auth()
    return redeem_invite(str(data.get("code") or ""), data.get("password"), h.headers.get("User-Agent", ""), me)


# ---- projects and their members
@route("GET", "/api/projects")
def api_projects(h, user, q, data):
    """My projects; with ?seen=slug:id,slug:id also how many messages from others came after what I saw."""
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
            d = dict(project_dict(p), last_id=last, members=n)
            if p["slug"] in seen:
                d["unread"] = db().execute("SELECT COUNT(*) FROM messages WHERE project_id = ? AND id > ? AND author != ? "
                                           "AND type NOT IN ('status', 'approval')",
                                           (p["id"], seen[p["slug"]], user["name"])).fetchone()[0]
            out.append(d)
    return {"projects": out}


@route("POST", "/api/projects")
def api_project_create(h, user, q, data):
    if user["kind"] != "human":
        raise ApiError(403, "проекты создают люди, не агенты")
    title = str(data.get("title") or "").strip()[:80]
    if not title:
        raise ApiError(400, "нужно название проекта")
    check_secret("название", title)
    slug = str(data.get("slug") or "").strip().lower() or slugify(title)
    if not SLUG_RE.match(slug):
        raise ApiError(400, "короткое имя проекта: латиница, цифры и -, от 2 до 40 символов")
    with DB_LOCK:
        con = db()
        pid = create_project_row(con, slug, title, user["name"])
        add_member(con, pid, user["name"], "owner", user["name"])
        p = con.execute("SELECT p.*, 'owner' AS role FROM projects p WHERE id = ?", (pid,)).fetchone()
    return {"project": project_dict(p)}


@route("POST", "/api/project/rename")
def api_project_rename(h, user, q, data):
    p = project_for(h, user, q)
    require_owner(p)
    title = str(data.get("title") or "").strip()[:80]
    if not title:
        raise ApiError(400, "нужно название")
    check_secret("название", title)
    with DB_LOCK:
        db().execute("UPDATE projects SET title = ? WHERE id = ?", (title, p["id"]))
    return {"ok": True}


@route("GET", "/api/members")
def api_members(h, user, q, data):
    p = project_for(h, user, q)
    with DB_LOCK:
        return {"project": project_dict(p), "members": users_with_presence(p["id"])}


def owners_left(pid, without):
    return db().execute("SELECT COUNT(*) FROM members WHERE project_id = ? AND role = 'owner' AND user != ?",
                        (pid, without)).fetchone()[0]


@route("POST", "/api/members/remove")
def api_member_remove(h, user, q, data):
    p = project_for(h, user, q)
    name = str(data.get("name") or "")
    if name != user["name"]:
        require_owner(p)     # anyone may leave; only owners remove others
    with DB_LOCK:
        role = member_role(p["id"], name)
        if not role:
            raise ApiError(404, f"{name} нет в проекте")
        if role == "owner" and not owners_left(p["id"], name):
            raise ApiError(409, "это последний владелец: сначала сделай владельцем кого-то ещё")
        db().execute("DELETE FROM members WHERE project_id = ? AND user = ?", (p["id"], name))
        # their agents in this project go too: an agent without its person makes no sense here
        db().execute("DELETE FROM members WHERE project_id = ? AND user IN "
                     "(SELECT name FROM users WHERE kind = 'agent' AND owner = ?)", (p["id"], name))
    return {"ok": True}


@route("POST", "/api/members/role")
def api_member_role(h, user, q, data):
    p = project_for(h, user, q)
    require_owner(p)
    name, role = str(data.get("name") or ""), data.get("role")
    if role not in ("owner", "member"):
        raise ApiError(400, "role: owner | member")
    with DB_LOCK:
        cur = member_role(p["id"], name)
        if cur not in ("owner", "member"):
            raise ApiError(404 if not cur else 400, f"{name} нет в проекте" if not cur else "роль бывает только у людей")
        if cur == "owner" and role == "member" and not owners_left(p["id"], name):
            raise ApiError(409, "это последний владелец")
        db().execute("UPDATE members SET role = ? WHERE project_id = ? AND user = ?", (role, p["id"], name))
    return {"ok": True}


@route("GET", "/api/summary")
def api_summary(h, user, q, data):
    """One round trip for agent hooks: am I listening, and what is still unacked."""
    p = project_for(h, user, q)
    with DB_LOCK:
        me = presence(user)
        unacked = list_messages(user, {"for_me": "1", "unacked": "1", "full": "0", "limit": "1000"}, p["id"])
        shown = unacked[-max(1, min(int(q.get("limit") or 10), 50)):]
        if shown:
            mark(user, [m["id"] for m in shown], "delivered")
        claims = [{k: c[k] for k in ("id", "key", "owner", "topic", "until", "created")}
                  for c in list_claims({"active": "1"}, p["id"])]
    return {"name": user["name"], "listening": me["listening"], "last_listen": me["last_listen"],
            "unacked_total": len(unacked), "unacked": shown, "last_id": LAST_ID[0], "claims": claims}


@route("GET", "/api/messages")
def api_list(h, user, q, data):
    p = project_for(h, user, q)
    with DB_LOCK:
        msgs = list_messages(user, q, p["id"])
        if q.get("for_me") == "1" and msgs:
            mark(user, [m["id"] for m in msgs], "delivered")
        return {"messages": msgs, "last_id": LAST_ID[0]}


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
        offline = [dict(name=u["name"], last_listen=u["last_listen"]) for u in users_with_presence(p["id"])
                   if u["name"] in expected and not u["listening"]]
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
        thread = list_messages(user, {"thread": m["thread_id"], "limit": 500, "after": "0"}, m["project_id"])
    return {"message": msg, "thread": thread}


@route("POST", r"/api/messages/(\d+)/status")
def api_status(h, user, q, data, mid):
    sid, _ = set_status(user, h.host(), mid, data)
    return {"ok": True, "status_message": sid}


@route("POST", r"/api/messages/(\d+)/approval")
def api_approval(h, user, q, data, mid):
    sid, _ = set_approval(user, h.host(), mid, data)
    return {"ok": True, "approval_message": sid}


@route("POST", "/api/receipts")
def api_receipts(h, user, q, data):
    kind = data.get("kind", "read")
    if kind not in ("read", "ack"):
        raise ApiError(400, "kind: read | ack")
    ids = [int(x) for x in data.get("ids") or []]
    with DB_LOCK:
        if data.get("all_for_me_upto"):
            p = project_for(h, user, q)
            ids = [m["id"] for m in list_messages(user, {"for_me": "1", "unacked": "1", "limit": 1000, "full": "0"}, p["id"])
                   if m["id"] <= int(data["all_for_me_upto"])]
        else:   # only messages of projects the user is in
            ids = [i for i in ids if (lambda r: r and member_role(r["project_id"], user["name"]))(
                db().execute("SELECT project_id FROM messages WHERE id = ?", (i,)).fetchone())]
    mark(user, ids, kind)
    return {"ok": True, "ids": ids}


@route("GET", "/api/wait")
def api_wait(h, user, q, data):
    after = int(q.get("after") or 0)
    timeout = max(0.0, min(float(q.get("timeout") or 25), 55))
    deadline = time.time() + timeout
    qq = {"after": str(after), "limit": "200", "for_me": q.get("for_me", "1"), "full": q.get("full", "1")}
    p = project_for(h, user, q)
    touch(user, waiting=True)
    while True:
        with NEW_MSG:
            seen = LAST_ID[0]
        with DB_LOCK:
            msgs = list_messages(user, qq, p["id"])
            if msgs and qq["for_me"] == "1":
                mark(user, [m["id"] for m in msgs], "delivered")
        if msgs:
            return {"messages": msgs, "last_id": seen}
        remaining = deadline - time.time()
        if remaining <= 0:
            return {"messages": [], "last_id": max(seen, after)}
        with NEW_MSG:
            if LAST_ID[0] == seen:
                NEW_MSG.wait(remaining)


@route("GET", r"/api/attachments/(\d+)")
def api_attachment(h, user, q, data, aid):
    with DB_LOCK:
        a = db().execute("SELECT a.*, m.project_id FROM attachments a JOIN messages m ON m.id = a.message_id "
                         "WHERE a.id = ?", (aid,)).fetchone()
        if a and not member_role(a["project_id"], user["name"]):
            a = None
    if not a:
        raise ApiError(404, "нет такого вложения")
    if a["content"] is None:
        raise ApiError(409, f"вложение по ссылке: {a['link']}")
    fname = urllib.parse.quote(a["name"])
    h.send_bytes(200, bytes(a["content"]), "application/octet-stream",
                 {"Content-Disposition": f"attachment; filename*=UTF-8''{fname}", "X-MD5": a["md5"] or ""})


@route("GET", "/api/board")
def api_board(h, user, q, data):
    p = project_for(h, user, q)
    with DB_LOCK:
        rows = db().execute("SELECT * FROM board WHERE project_id = ? ORDER BY name", (p["id"],)).fetchall()
    return {"board": [{"name": r["name"], "now": r["now"], "next": r["next"], "gpu_until": r["gpu_until"],
                       "note": r["note"], "time": iso(r["ts"])} for r in rows]}


@route("POST", "/api/board")
def api_board_set(h, user, q, data):
    p = project_for(h, user, q)
    vals = {}
    for k in ("now", "next", "gpu_until", "note"):
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
        raise ApiError(400, "нужен key — что именно занимаешь")
    check_secret("key", display)
    p = project_for(h, user, q)
    vals = {"topic": claim_text(data, "topic", 60), "note": claim_text(data, "note", 500),
            "until": claim_text(data, "until", 100)}
    with DB_LOCK:
        con = db()
        cur = con.execute("SELECT * FROM claims WHERE key = ? AND released IS NULL AND project_id = ? ORDER BY id DESC",
                          (key, p["id"])).fetchone()
        taken = None
        if cur and cur["owner"] == user["name"]:      # same owner again: idempotent, only refresh what was passed
            for k, v in vals.items():
                if data.get(k) is not None:
                    con.execute(f"UPDATE claims SET {k} = ? WHERE id = ?", (v if k == "topic" else (v or None), cur["id"]))
            row = con.execute("SELECT * FROM claims WHERE id = ?", (cur["id"],)).fetchone()
            return {"claim": claim_dict(row), "created": False}
        if cur:
            if not data.get("force"):
                raise ApiError(409, f"«{cur['display_key']}» уже занято: {cur['owner']}", holder=claim_dict(cur))
            taken = release_claim(cur, f"перехвачено {user['name']}")
        cid = con.execute("INSERT INTO claims(key, display_key, topic, owner, owner_kind, host, note, until, created,"
                          " project_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (key, display, vals["topic"], user["name"], user["kind"], h.host(), vals["note"] or None,
                           vals["until"] or None, time.time(), p["id"])).lastrowid
        res = {"claim": claim_dict(con.execute("SELECT * FROM claims WHERE id = ?", (cid,)).fetchone()), "created": True}
        if taken:
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
                raise ApiError(400, "id должен быть числом")
            if not row:
                raise ApiError(404, f"заявки #{data['id']} нет")
            if row["released"] is not None:
                raise ApiError(409, f"заявка #{row['id']} «{row['display_key']}» уже снята")
        elif data.get("key"):
            rows = con.execute("SELECT * FROM claims WHERE key = ? AND released IS NULL AND project_id = ? ORDER BY id DESC",
                               (norm_key(data["key"]), p["id"])).fetchall()
            if not rows:
                raise ApiError(404, f"активной заявки «{str(data['key'])[:80]}» нет")
            row = next((r for r in rows if r["owner"] == user["name"]), rows[0])
        else:
            raise ApiError(400, "нужен key или id")
        if row["owner"] != user["name"]:
            if user["kind"] != "human":
                raise ApiError(403, f"«{row['display_key']}» занял {row['owner']}: чужую заявку снимает только человек")
            note = note or f"снято {user['name']}"
        return {"claim": release_claim(row, note)}


# ---------------------------------------------------------------- CLI
def cmd_adduser(a):
    if not NAME_RE.match(a.name):
        sys.exit("имя: латиница/цифры/._- до 32 символов")
    tok = "tgt_" + secrets.token_hex(24)
    with DB_LOCK:
        con = db()
        if con.execute("SELECT 1 FROM users WHERE name = ?", (a.name,)).fetchone():
            if not a.rotate:
                sys.exit(f"{a.name} уже есть; --rotate выдаст новый токен (старый перестанет работать)")
            con.execute("UPDATE users SET token_hash = ?, revoked = 0, kind = ?, owner = ? WHERE name = ?",
                        (token_hash(tok), a.kind, a.owner or a.name, a.name))
        else:
            con.execute("INSERT INTO users(name, kind, owner, token_hash, created_ts) VALUES (?,?,?,?,?)",
                        (a.name, a.kind, a.owner or a.name, token_hash(tok), time.time()))
            # into a project: the given one, or the default (created on first use; its first person owns it)
            pid = cli_project(con, a.project, create=not a.project)
            first_human = a.kind == "human" and not con.execute(
                "SELECT 1 FROM members WHERE project_id = ? AND role = 'owner'", (pid,)).fetchone()
            add_member(con, pid, a.name, "owner" if first_human else "member", "cli")
    if a.token_out:
        os.makedirs(os.path.dirname(os.path.abspath(a.token_out)), exist_ok=True)
        with open(a.token_out, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
        print(f"{a.name} ({a.kind}, owner {a.owner or a.name}): токен записан в {a.token_out}")
    else:
        print(f"{a.name} ({a.kind}, owner {a.owner or a.name}). Токен (показывается один раз):\n{tok}")


def cmd_users(a):
    sessions = {r[0]: r[1] for r in db().execute("SELECT user, COUNT(*) FROM sessions GROUP BY user")}
    for r in db().execute("SELECT * FROM users ORDER BY kind, name"):
        pw = ("админ, " if r["is_admin"] else "") + ("пароль задан" if r["pw_hash"] else "")
        print(f"{r['name']:<20} {r['kind']:<6} owner={r['owner']:<10} {pw:<20} сессий: {sessions.get(r['name'], 0)}"
              f"{'  REVOKED' if r['revoked'] else ''}")


def cmd_revoke(a):
    db().execute("UPDATE users SET revoked = 1 WHERE name = ?", (a.name,))
    db().execute("DELETE FROM sessions WHERE user = ?", (a.name,))
    print(f"{a.name}: токен отозван, сессии закрыты")


def cmd_admin(a):
    for name in a.names:
        n = db().execute("UPDATE users SET is_admin = ? WHERE name = ? AND kind = 'human'", (0 if a.off else 1, name)).rowcount
        print(f"{name}: {'не найден среди людей' if not n else ('больше не админ' if a.off else 'админ')}")


def cli_project(con, slug, create=False):
    slug = slug or Cfg.default_project["slug"]
    r = con.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
    if r:
        return r["id"]
    if not create:
        sys.exit(f"проекта {slug} нет; есть: " + ", ".join(x["slug"] for x in con.execute("SELECT slug FROM projects")))
    return create_project_row(con, slug, Cfg.default_project["title"] if slug == Cfg.default_project["slug"] else slug, "cli")


def cmd_projects(a):
    con = db()
    for p in con.execute("SELECT * FROM projects ORDER BY id"):
        ms = con.execute("SELECT user, role FROM members WHERE project_id = ? ORDER BY role, user", (p["id"],)).fetchall()
        print(f"{p['slug']:<20} «{p['title']}»{'  (архив)' if p['archived'] else ''}")
        print("    " + ", ".join(f"{m['user']} ({m['role']})" for m in ms))


def cmd_addmember(a):
    con = db()
    pid = cli_project(con, a.project)
    if not con.execute("SELECT 1 FROM users WHERE name = ?", (a.name,)).fetchone():
        sys.exit(f"{a.name} нет")
    try:
        add_member(con, pid, a.name, a.role, "cli")
    except ApiError as e:
        sys.exit(e.msg)
    print(f"{a.name} теперь в проекте {a.project} ({member_role(pid, a.name)})")


def cmd_invite(a):
    con = db()
    exists = con.execute("SELECT 1 FROM users WHERE name = ?", (a.name,)).fetchone()
    if exists and not a.project:
        inv = create_invite("cli", a.name, reset=True)
    else:
        inv = create_invite("cli", a.name, a.kind, a.owner, cli_project(con, a.project))
    what = {"new": "новый участник", "add": "в проект",
            "reset": "сброс пароля" if inv["kind"] == "human" else "новый токен агента"}[inv["purpose"]]
    print(f"{inv['name']} ({inv['kind']}, {what}), действует до {inv['expires']}")
    print((Cfg.launcher_url + "#invite=" + inv["code"]) if Cfg.launcher_url else f"код: {inv['code']} (добавь к адресу: /#invite=КОД)")


def cmd_clearpassword(a):
    db().execute("UPDATE users SET pw_hash = NULL WHERE name = ?", (a.name,))
    n = db().execute("DELETE FROM sessions WHERE user = ?", (a.name,)).rowcount
    print(f"{a.name}: пароль сброшен, закрыто сессий: {n}. Войти можно по ссылке с токеном и задать новый.")


def cmd_serve(a):
    db()
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
    s.set_defaults(fn=cmd_addmember)
    s = sub.add_parser("users")
    s.set_defaults(fn=cmd_users)
    s = sub.add_parser("revoke")
    s.add_argument("name")
    s.set_defaults(fn=cmd_revoke)
    s = sub.add_parser("clearpassword", help="сбросить пароль человека и закрыть его сессии")
    s.add_argument("name")
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
    s.set_defaults(fn=cmd_invite)
    a = p.parse_args()
    load_config(a.config)
    if a.db:
        Cfg.db = a.db
    a.fn(a)


if __name__ == "__main__":
    main()
