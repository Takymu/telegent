#!/usr/bin/env python3
"""tg.py: telegent client for agents (and humans who like the terminal). Stdlib only, never prompts.

    python tg.py send "текст" -t request -T tuning --to bob-agent
    python tg.py inbox | read 42 | reply 42 "ответ" | ack 42 | watch
    python tg.py take 42 | done 42 --ref "commit abc123" | reject 42 --note "почему"
    python tg.py claim "resnet18 lr3e-4" --until "03:00" | claims | release "resnet18 lr3e-4"

Config (first found): --config, $TELEGENT_CONFIG, tg.config.json next to this file, ~/.telegent/config.json
    {"server": "https://...", "token_file": "~/.telegent/token", "secret_files": ["C:/work/project/secrets.txt"],
     "watch_cmd": "python C:/path/tg.py watch"}      # watch_cmd: optional, the hook suggests exactly this command
Full protocol: AGENTS.md.
"""
import argparse
import base64
import codecs
import hashlib
import http.client
import contextlib
import io
import json
import os
import platform
import re
import socket
import shlex
import shutil
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
INLINE_LIMIT = 2 * 1024 * 1024
MAX_FILE_BYTES = 200 * 1024 * 1024
# The server's tunnel address changes when the phone restarts it; the phone publishes the new one here
# (termux/publish_url.sh). Override with "url_source" in the config, "" turns the lookup off.
URL_SOURCE = "https://raw.githubusercontent.com/Takymu/telegent/gh-pages/url.txt"
# the same file through the GitHub API: fresh within a minute (raw.githubusercontent.com may serve a
# copy up to 5 minutes old), but limited to 60 requests an hour per IP, so raw stays the fallback
URL_SOURCE_API = "https://api.github.com/repos/Takymu/telegent/contents/url.txt?ref=gh-pages"
GH_REPO = "Takymu/telegent"
RELEASE_FEED = "https://takymu.github.io/telegent/releases.json"
CLIENT_VERSION = "2026.10.21.1"
UPDATE_INTERVAL = 6 * 3600
_last_discovery = [0.0]
PROJECT_ARG = [None]   # --project, set in main()


def release_bytes(url, limit):
    # Release requests never carry the team's API credentials.
    request = urllib.request.Request(url, headers={"User-Agent": "telegent-updater/1", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=15) as response:
        if not response.geturl().startswith("https://"):
            raise ValueError("обновление требует HTTPS")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("слишком большой файл обновления")
    return data


def validate_client_release(doc):
    release = doc['client']
    commit = release['commit']
    if not re.fullmatch(r'[0-9a-f]{40}', commit) or not isinstance(release.get('version'), str):
        raise ValueError('некорректный выпуск клиента')
    for name, item in release['files'].items():
        if name not in ('tg.py', 'AGENTS.md', 'MANIFEST.md') or not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise ValueError('некорректный файл выпуска')
    if 'tg.py' not in release['files']:
        raise ValueError('в выпуске нет tg.py')
    return release


def apply_client_release(release, client_path):
    """Fast-forward a clean official clone, or replace recognized standalone files atomically."""
    from pathlib import Path
    client = Path(client_path).resolve()
    directory = client.parent
    git = subprocess.run(['git', '-C', str(directory), 'rev-parse', '--show-toplevel'],
                         capture_output=True, text=True, timeout=10) if shutil.which('git') else None
    if git and git.returncode == 0:
        root = Path(git.stdout.strip()).resolve()
        def command(*parts):
            return subprocess.run(['git', '-C', str(root), *parts], capture_output=True, text=True, timeout=45)
        if client != root/'tg.py':
            raise ValueError('клиент внутри другого git-проекта; локальные файлы сохранены')
        remote = command('remote', 'get-url', 'origin').stdout.strip()
        if remote not in ('https://github.com/Takymu/telegent.git', 'https://github.com/Takymu/telegent',
                          'git@github.com:Takymu/telegent.git', 'https://Takymu@github.com/Takymu/telegent.git'):
            raise ValueError('у клона другой источник; локальные файлы сохранены')
        if command('status', '--porcelain', '--untracked-files=no').stdout.strip():
            raise ValueError('в клоне есть локальные изменения; они сохранены')
        if command('symbolic-ref', '--short', 'HEAD').stdout.strip() != 'main':
            raise ValueError('клиент на рабочей ветке; она сохранена')
        if command('fetch', '--no-tags', 'https://github.com/Takymu/telegent.git', release['commit']).returncode:
            raise ValueError('не удалось получить выпуск клиента')
        if command('merge-base', '--is-ancestor', 'HEAD', release['commit']).returncode:
            raise ValueError('локальная история расходится с выпуском; она сохранена')
        # Recheck after the network operation: a developer may have started editing meanwhile.
        if command('status', '--porcelain', '--untracked-files=no').stdout.strip():
            raise ValueError('в клоне появились локальные изменения; они сохранены')
        if command('merge', '--ff-only', release['commit']).returncode:
            raise ValueError('не удалось обновить клон без потери локальных изменений')
    else:
        staged = []
        try:
            for name, item in release['files'].items():
                path = directory/name
                if path.is_symlink():
                    raise ValueError('символическая ссылка оставлена без изменений')
                before = path.read_bytes() if path.exists() else None
                digest = hashlib.sha256(before).hexdigest() if before is not None else None
                if digest == item['sha256']:
                    continue
                if before is not None and digest not in item.get('previous_sha256', []):
                    if name != 'tg.py': continue  # Project instructions belong to their author.
                    raise ValueError('локальный файл изменён: '+name+'; он сохранён')
                data = release_bytes('https://raw.githubusercontent.com/'+GH_REPO+'/'+release['commit']+'/'+name, 2*1024*1024)
                if hashlib.sha256(data).hexdigest() != item['sha256']:
                    raise ValueError('не совпала SHA-256 файла '+name)
                if name == 'tg.py': compile(data, 'tg.py', 'exec')
                fd, temporary = tempfile.mkstemp(prefix='.telegent-update-', dir=directory)
                with os.fdopen(fd, 'wb') as stream: stream.write(data)
                if path.exists(): os.chmod(temporary, path.stat().st_mode)
                staged.append((path, temporary, before))
            if any((path.read_bytes() if path.exists() else None) != before for path, _, before in staged):
                raise ValueError('локальные файлы изменились во время загрузки; обновление отменено')
            # The executable is replaced last; a failed download never installs a partial client.
            for path, temporary, _ in sorted(staged, key=lambda row: row[0].name == 'tg.py'):
                os.replace(temporary, path)
        finally:
            for _, temporary, _ in staged:
                if os.path.exists(temporary): os.unlink(temporary)


def update_cache():
    identity = hashlib.sha256(os.path.abspath(__file__).encode('utf-8')).hexdigest()[:20]
    return os.path.join(os.path.expanduser('~'), '.telegent', 'updates', identity)


def update_settings(conf):
    if not conf.path: return {}
    with open(conf.path, encoding='utf-8-sig') as stream: return json.load(stream)


def client_digest():
    with open(__file__, 'rb') as stream: return hashlib.sha256(stream.read()).hexdigest()


def cmd_update(conf, args):
    folder = update_cache(); os.makedirs(folder, exist_ok=True)
    lock = os.path.join(folder, 'lock')
    try:
        if os.path.exists(lock) and time.time()-os.path.getmtime(lock) > 600: os.unlink(lock)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
    except FileExistsError:
        return
    status = {'checked_at': time.time()}
    try:
        release = validate_client_release(json.loads(release_bytes(RELEASE_FEED, 65536)))
        current = client_digest()
        if current != release['files']['tg.py']['sha256']:
            apply_client_release(release, __file__)
        status.update(version=release['version'], status='current')
        if not args.automatic: print('Клиент обновлён: '+release['version']+'. Конфиги, токены и хуки сохранены.')
    except Exception as error:
        status.update(status='deferred', reason=str(error))
        if not args.automatic: die('обновление отложено: '+str(error))
    finally:
        write_json_atomic(os.path.join(folder, 'status.json'), status)
        os.unlink(lock)


def schedule_update(conf):
    """Check in a separate process so unavailable releases cannot delay a message or hook."""
    try:
        settings = update_settings(conf)
        if settings.get('auto_update', True) is False or not conf.url_source or os.environ.get('TELEGENT_URL'):
            return
        folder = update_cache(); os.makedirs(folder, exist_ok=True)
        marker = os.path.join(folder, 'scheduled')
        if os.path.exists(marker) and time.time()-os.path.getmtime(marker) < UPDATE_INTERVAL: return
        with open(marker, 'w'): pass
        command = [sys.executable, os.path.abspath(__file__), '--config', conf.path, 'update', '--automatic']
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    except (OSError, ValueError, TypeError):
        pass

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Same list as server.py: the client refuses first, the server enforces.
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


def die(msg, code=1):
    print(f"tg: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- config
def repo_config():
    """.telegent.json in the current directory or above: one identity per project folder
    (two agents on one machine work in two repos)."""
    d = os.getcwd()
    while True:
        p = os.path.join(d, ".telegent.json")
        if os.path.exists(p):
            return p
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def config_path(explicit=None):
    for p in (explicit, os.environ.get("TELEGENT_CONFIG"), repo_config(), os.path.join(HERE, "tg.config.json"),
              os.path.join(os.path.expanduser("~"), ".telegent", "config.json")):
        if p and os.path.exists(os.path.expanduser(p)):
            return os.path.expanduser(p)
    return None


def resolve(path, base):
    path = os.path.expanduser(path)
    if re.match(r"^[A-Za-z]:[\\/]", path) and os.name != "nt":      # C:/x from WSL -> /mnt/c/x
        path = "/mnt/" + path[0].lower() + "/" + path[3:].replace("\\", "/")
    return path if os.path.isabs(path) else os.path.join(base, path)


class Conf:
    def __init__(self, explicit=None):
        self.path = config_path(explicit)
        c = {}
        if self.path:
            with open(self.path, encoding="utf-8-sig") as f:
                c = json.load(f)
        base = os.path.dirname(self.path) if self.path else HERE
        self.server = (os.environ.get("TELEGENT_URL") or c.get("server") or "").rstrip("/")
        self.url_source = "" if os.environ.get("TELEGENT_URL") else c.get("url_source", URL_SOURCE)
        token = c.get("token")
        tf = os.environ.get("TELEGENT_TOKEN_FILE") or c.get("token_file")
        if tf:
            try:
                with open(resolve(tf, base), encoding="utf-8-sig") as f:
                    token = f.read().strip()
            except OSError as e:
                die(f"не читается token_file {tf}: {e.strerror}")
        self.token = token
        self.state_path = resolve(c.get("state_file") or "tg.state.json", base)
        self.watch_cmd = (c.get("watch_cmd") or "").strip() or None
        # Secretaries aggregate incoming messages; writes still select a project explicitly.
        self.secretary = bool(c.get('is_secretary'))
        self.project = os.environ.get("TELEGENT_PROJECT") or PROJECT_ARG[0] or c.get("project") or None
        self.literals = []
        for entry in c.get("secret_files") or []:
            if isinstance(entry, dict):
                path, lines = entry.get("path"), set(entry.get("lines") or [])
            else:
                path, lines = entry, set()
            try:
                with open(resolve(path, base), encoding="utf-8-sig", errors="replace") as f:
                    for i, ln in enumerate(f, 1):
                        if (not lines or i in lines) and len(ln.strip()) >= 6:
                            self.literals.append(ln.strip())
            except OSError:
                print(f"tg: предупреждение: secret_files {path} не читается", file=sys.stderr)
        if not self.server or not self.token:
            die("нет настройки: нужен tg.config.json с server и token_file (см. README.md), "
                f"искал: --config, $TELEGENT_CONFIG, {os.path.join(HERE, 'tg.config.json')}, ~/.telegent/config.json")

    def load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_server(self, url):
        """Remember a new server address in the config file (other fields untouched)."""
        if not self.path:
            return
        with open(self.path, encoding="utf-8-sig") as f:
            c = json.load(f)
        c["server"] = url
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=2)
        if not replace_file(tmp, self.path):
            print(f"tg: предупреждение: не удалось записать {self.path} (файл занят другим процессом)", file=sys.stderr)

    def save_state(self, st):
        # a second watch on the same state file must not kill this one: the state is rewritten on the next message anyway
        tmp = f"{self.state_path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        replace_file(tmp, self.state_path)


def replace_file(tmp, path, tries=8):
    """os.replace that survives a Windows sharing violation: another process (a second watch, an antivirus scan) may
    hold the destination open for a moment, and os.replace then raises PermissionError. The temp name is per process
    (two writers never share it); retry briefly, then give up quietly and drop the temp file."""
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def host_tag():
    rel = platform.release().lower()
    kind = "wsl" if "microsoft" in rel or "wsl" in rel else ("win" if os.name == "nt" else sys.platform)
    return f"{socket.gethostname()}/{kind}"


# ---------------------------------------------------------------- HTTP
class NetError(Exception):
    pass


class NoApiRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an account credential to a proxy's login/redirect target.
        return None


class Conflict(Exception):
    """HTTP 409 that carries a claim holder (claim refused)."""
    def __init__(self, msg, holder):
        super().__init__(msg)
        self.holder = holder


def api(conf, method, path, data=None, timeout=30, raw=False, sink=None):
    if (getattr(conf,'secretary',False) and not conf.project and method=='GET'
            and path.split('?')[0] in ('/api/messages','/api/summary','/api/wait')):
        path+=('&' if '?' in path else '?')+'projects=all'
    url = conf.server + path
    body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {conf.token}", "X-Telegent-Host": host_tag(),
        "Content-Type": "application/octet-stream" if isinstance(data, bytes) else "application/json", "User-Agent": "telegent-tg/1.0",
        **({"X-Telegent-Project": conf.project} if conf.project else {})})
    try:
        with urllib.request.build_opener(NoApiRedirect).open(req, timeout=timeout) as r:
            if sink is not None:
                digest, size = hashlib.md5(), 0
                with open(sink, "wb") as f:
                    while True:
                        chunk = r.read(256*1024)
                        if not chunk:
                            break
                        f.write(chunk); digest.update(chunk); size += len(chunk)
                if r.headers.get("Content-Length") and size != int(r.headers["Content-Length"]):
                    raise NetError("файл получен не полностью")
                return {"size": size, "md5": digest.hexdigest()}
            payload = r.read()
            if raw:return payload,dict(r.headers)
            try:return json.loads(payload.decode('utf-8'))
            except (ValueError,UnicodeError):raise NetError('сервер вернул неполный или неверный JSON')
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
            msg = err.get("error")
        except Exception:
            err, msg = {}, None
        if e.code == 409 and err.get("holder"):
            raise Conflict(msg, err["holder"])
        if e.code >= 500 and e.code != 507:
            raise NetError(f"сервер недоступен (HTTP {e.code}{': ' + msg if msg else ''})")
        die(f"сервер ответил {e.code}: {msg or e.reason}", 2)
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as e:
        raise NetError(str(getattr(e, "reason", e)))


def fetch_published(source, timeout=10):
    """The address published at source (a url.txt), or None."""
    sep = "&" if "?" in source else "?"
    req = urllib.request.Request(f"{source}{sep}t={int(time.time())}",
                                 headers={"Accept": "application/vnd.github.raw", "User-Agent": "telegent-tg/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            url = r.read(500).decode("utf-8", "replace").strip().rstrip("/")
    except Exception:
        return None
    return url if re.match(r"^(https://[A-Za-z0-9.-]+|http://(127\.0\.0\.1|localhost)(:\d+)?)$", url) else None


def fetch_published_git(timeout=10):
    """Fresh and free of the REST API's hourly limit: the gh-pages commit from git's own ref listing
    (what `git ls-remote` reads), then url.txt of exactly that commit, which no cache can have stale."""
    req = urllib.request.Request(f"https://github.com/{GH_REPO}.git/info/refs?service=git-upload-pack",
                                 headers={"User-Agent": "git/2.45 telegent-tg"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            refs = r.read(500000).decode("latin-1")
    except Exception:
        return None
    m = re.search(r"([0-9a-f]{40}) refs/heads/gh-pages", refs)
    return fetch_published(f"https://raw.githubusercontent.com/{GH_REPO}/{m.group(1)}/url.txt", timeout=timeout) if m else None


def published_candidates(source, timeout=10):
    """Addresses from the freshest source to the most available one (lazily: stop at the first useful)."""
    if not source:
        return
    if source == URL_SOURCE:
        yield fetch_published_git(timeout=timeout)
        yield fetch_published(URL_SOURCE_API, timeout=timeout)
    yield fetch_published(source, timeout=timeout)


def discover_server(conf, min_interval=30, timeout=10):
    """Look up the published server address; switch to it (and save it in the config) if it changed.
    Called when the server is unreachable. Returns True if the address changed."""
    if not conf.url_source or time.time() - _last_discovery[0] < min_interval:
        return False
    _last_discovery[0] = time.time()
    url = None
    for found in published_candidates(conf.url_source, timeout=timeout):
        if found and found != conf.server:
            try:
                # Publication can lag or return a dead old tunnel. Probe without credentials.
                with urllib.request.urlopen(found+'/api/info',timeout=timeout) as response:
                    info=json.loads(response.read(65536).decode('utf-8'))
                if not isinstance(info,dict) or not info.get('version') or not isinstance(info.get('types'),list):continue
            except (OSError,ValueError,UnicodeError,http.client.HTTPException):continue
            url = found;break
    if not url:
        return False
    old, conf.server = conf.server, url
    try:
        conf.save_server(url)
        saved = "конфиг обновлён"
    except (OSError, ValueError) as e:
        saved = f"конфиг НЕ обновлён: {e}"
    print(f"tg: сервер переехал: {old} -> {url} ({saved})", file=sys.stderr, flush=True)
    return True


def api_retry(conf, method, path, data=None, tries=4, **kw):
    """Retry on network errors only. Sends are safe to retry: they carry an idempotency key.
    If the server is gone, look up its new published address first."""
    delay,attempt,extra = 1.0,0,False
    tries=max(1,tries)
    while attempt<tries:
        attempt+=1
        try:
            return api(conf, method, path, data, **kw)
        except NetError as e:
            moved=discover_server(conf)
            # A relocation on the final attempt still needs an actual request to the new host.
            if moved and attempt==tries and not extra:
                tries+=1;extra=True
            if attempt==tries:
                die(f"нет связи с {conf.server}: {e}", 3)
            if not moved:
                time.sleep(delay)
                delay *= 2


# ---------------------------------------------------------------- formatting
def short_time(iso):
    # 2026-10-07T01:23:45+07:00 -> 10-07 01:23+07
    if not iso:
        return ""
    return f"{iso[5:10]} {iso[11:16]}{iso[19:22]}"


def span(sec):
    """Seconds -> '5 мин' / '3 ч' / '2 дн'."""
    sec = abs(sec)
    if sec < 3600:
        return f"{max(1, round(sec / 60))} мин"
    if sec < 86400:
        return f"{round(sec / 3600)} ч"
    return f"{round(sec / 86400)} дн"


def who(m):
    kind = "HUMAN" if m["author_kind"] == "human" else "secretary" if m.get('author_is_secretary') else "agent"
    return f"{m['author']}({kind}@{m['host'] or '?'})"


def oneline(m, width=220):
    parts = [f"#{m['id']}", short_time(m["time"])]
    if m.get('project'):parts.append('project='+m['project'])
    if m.get("deleted"):parts.append("УДАЛЕНО")
    elif m.get("edited"):parts.append("ИЗМЕНЕНО")
    if m.get("conversation_id"):
        parts.append("ЛИЧНОЕ")
    if m["priority"] == "urgent":
        parts.append("URGENT")
    parts.append(m["type"])
    parts.append(who(m) + (" -> " + ",".join(m["to"]) if m["to"] else ""))
    if m["topic"]:
        parts.append(f"[{m['topic']}]")
    if m["reply_to"]:
        parts.append(f"re #{m['reply_to']}")
    if m["needs_approval"]:
        parts.append("NEEDS-HUMAN-APPROVAL" + (f"({m['approver']})" if m.get("approver") else ""))
    if m["status"]:
        parts.append(f"{{{m['status']['state']}}}")
    if m["attachments"]:
        parts.append(f"+{len(m['attachments'])}att")
    first_body = next((ln.strip() for ln in (m["body"] or "").splitlines() if ln.strip()), "")
    if m["type"] in ("status", "approval"):   # their line is the subject, the body is a note
        text = m["subject"] + (" | " + first_body if first_body else "")
    else:
        text = first_body or m["subject"]
    line = " ".join(parts) + ": " + text
    line = line.replace("\r", " ").replace("\n", " ")
    return line if len(line) <= width else line[:width - 1] + "…"


def approval_lines(m, me):
    out = []
    if not m["needs_approval"]:
        return out
    if not m["approvals"]:
        who_decides = f"ждёт решения {m['approver']}" if m.get("approver") else "нужно одобрение человека"
        out.append(f"ОДОБРЕНИЕ: {who_decides}, решений пока нет — НЕ выполнять.")
    for a in m["approvals"]:
        word = "ОДОБРЕНО" if a["decision"] == "approve" else "ОТКАЗАНО"
        out.append(f"ОДОБРЕНИЕ: {word} {a['by']} {short_time(a['time'])}" + (f": {a['note']}" if a["note"] else ""))
    return out


def receipt_str(r):
    def t(x):
        return short_time(x)[6:] if x else "—"
    if not r["delivered"]:
        return f"{r['user']}: ещё не доставлено"
    return f"{r['user']}: доставлено {short_time(r['delivered'])}, прочитано {t(r['read'])}, ack {t(r['acked'])}"


def print_full(m, me):
    print("=" * 78)
    print(oneline(m, width=10_000))
    print(f"время: {m['time']}   автор: {who(m)}, владелец {m['owner']}   кому: {', '.join(m['to']) or 'всем'}")
    if m.get("edited") and not m.get("deleted"):
        print(f"изменено: {m['edited']}   версия: {m.get('revision',0)}")
    if m["author_kind"] == "agent" and m["author"] != me.get("name"):
        print("(рабочая переписка и просьбы коллег)")
    if m["status"]:
        s = m["status"]
        print(f"статус: {s['state']} ({s['by']}, {short_time(s['time'])})"
              + (f" ref: {s['ref']}" if s.get("ref") else "") + (f" — {s['note']}" if s.get("note") else ""))
    for ln in approval_lines(m, me):
        print(ln)
    if m["result"]:
        print("результат: " + ", ".join(f"{k}={v}" for k, v in m["result"].items()))
    for r in m.get("receipts") or []:
        print("получение: " + receipt_str(r))
    for r in m.get("reactions") or []:
        print(f"реакция {r['emoji']}: {r['count']} ({', '.join(r['users'])})")
    print("-" * 78)
    print(m["body"] or m["subject"])
    for a in m["attachments"]:
        size = f"{a['size']} B" if a.get("size") is not None else "? B"
        where = "в сообщении" if a["inline"] else f"ссылка: {a['link']}"
        print(f"  вложение {a['id']}: {a['name']}  {size}  md5={a['md5'] or '?'}  ({where})")
    if any(a["inline"] for a in m["attachments"]):
        print(f"  скачать: python tg.py get {m['id']} --out <папка>")
    if m.get("secret"):
        print(f"  секрет: {m['secret']['state']}; срок: {datetime.fromtimestamp(m['secret']['expires']).isoformat(timespec='seconds')}")
        if m["secret"]["state"] == "pending":
            print(f"  получить в файл: python tg.py secret {m['id']} --out <локальный-файл>")


# ---------------------------------------------------------------- commands
def get_me(conf):
    return api_retry(conf, "GET", "/api/me")


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


def decode_message_text(raw, encoding="auto"):
    if encoding.lower() == "auto":
        # UTF-32 BOMs start with a UTF-16 BOM, so check the longer signatures first.
        encoding = next((enc for bom,enc in ((codecs.BOM_UTF32_LE,"utf-32"),(codecs.BOM_UTF32_BE,"utf-32"),
                        (codecs.BOM_UTF16_LE,"utf-16"),(codecs.BOM_UTF16_BE,"utf-16"),(codecs.BOM_UTF8,"utf-8-sig"))
                         if raw.startswith(bom)), "utf-8")
    try:
        return raw.decode(encoding,errors="strict").lstrip("\ufeff").replace("\r\n","\n").replace("\r","\n")
    except (LookupError,UnicodeError,TypeError):
        die("НЕ отправлено: не удалось прочитать текст в выбранной кодировке. "
            "Укажи --encoding (например cp1251, cp866, koi8-r, cp1252, latin-1, utf-16-le) "
            "или сохрани исходный текст в UTF-8. Авто: UTF-8 и UTF-16/UTF-32 с BOM.",2)


def read_body(args):
    if getattr(args, "file", None):
        if args.file == "-":
            if hasattr(sys.stdin,"buffer"):
                return decode_message_text(sys.stdin.buffer.read(),getattr(args,"encoding","auto"))
            return sys.stdin.read()
        try:
            with open(args.file,"rb") as f:
                return decode_message_text(f.read(),getattr(args,"encoding","auto"))
        except OSError:
            die("не удалось открыть файл сообщения",2)
    return args.body or ""


def scan_local(conf, field, text):
    if not text:
        return
    for kind, rx in _SECRET_RES:
        m = rx.search(text)
        if m:
            die(f"НЕ отправлено: похоже на секрет ({kind}) в поле «{field}», строка {text.count(chr(10), 0, m.start()) + 1}. "
                "Убери значение (напиши «лежит в файле X у меня») и пошли снова.", 4)
    for lit in conf.literals:
        i = text.find(lit)
        if i >= 0:
            die(f"НЕ отправлено: в поле «{field}» (строка {text.count(chr(10), 0, i) + 1}) есть строка из secret_files.", 4)


def build_attachments(conf, args, send_key):
    atts = []
    for path in args.attach or []:
        p = os.path.expanduser(path)
        if not os.path.isfile(p):
            die(f"нет файла {path}")
        size = os.path.getsize(p)
        name = os.path.basename(p)
        if not args.no_inline and size > MAX_FILE_BYTES:
            die(f"файл {name}: максимальный размер — 200 МБ", 2)
        scan_local(conf, "имя вложения", name)
        digest, tail = hashlib.md5(), ""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        overlap = max([4096] + [len(x)+512 for x in conf.literals])
        with open(p, "rb") as f:
            while True:
                chunk = f.read(256*1024)
                text = tail + decoder.decode(chunk, final=not chunk)
                if not args.no_inline:
                    scan_local(conf, "вложение " + name, text)
                if not chunk:
                    break
                digest.update(chunk); tail = text[-overlap:]
        a = {"name": name, "size": size, "md5": digest.hexdigest()}
        if args.no_inline:
            a["link"] = f"{host_tag()}:{os.path.abspath(p)}"
        else:
            a["_path"] = p
        atts.append(a)
    for spec in args.link or []:
        # URL_or_path[#md5]
        link, _, md5 = spec.partition("#")
        atts.append({"name": os.path.basename(link.rstrip("/")) or "link", "link": link, "md5": md5 or None})
    if len(atts) > 30:
        die("не больше 30 вложений", 2)
    for a in atts:
        scan_local(conf, "ссылка", a.get("link") or "")
    # Scan every local file before any bytes leave the machine.
    for index, a in enumerate(atts):
        if "_path" not in a:
            continue
        key = hashlib.sha256(json.dumps([send_key, index, a['name'], a['md5']]).encode()).hexdigest()
        upload = api_retry(conf, "POST", "/api/uploads", {"name": a["name"], "size": a["size"], "md5": a["md5"], "key": key})
        uid, offset = upload["id"], upload["received"]
        if not upload["complete"]:
            step, last_progress = min(upload["chunk_size"], 4*1024*1024), -1
            with open(a["_path"], "rb") as f:
                f.seek(offset)
                while offset < a["size"]:
                    chunk = f.read(min(step, a["size"]-offset))
                    if not chunk:
                        die(f"файл {a['name']} изменился во время отправки; повтори команду", 2)
                    result = api_retry(conf, "POST", f"/api/uploads/{uid}/chunk?offset={offset}", chunk, timeout=120)
                    offset = result["received"]
                    f.seek(offset)
                    progress = int(offset*100/max(1, a["size"]))
                    if progress//20 != last_progress:
                        print(f"tg: {a['name']}: {progress}%", file=sys.stderr)
                        last_progress = progress//20
            api_retry(conf, "POST", f"/api/uploads/{uid}/complete", {}, timeout=180)
        a.clear(); a["upload_id"] = uid
    return atts


def build_result(args):
    r = {k: getattr(args, k) for k in ("metric", "dataset", "value", "err", "baseline", "commit", "weights")
         if getattr(args, k, None) not in (None, "")}
    for k in ("value", "err"):
        if k in r:
            try:
                r[k] = float(r[k])
            except ValueError:
                pass
    return r or None


def do_send(conf, args, reply_to=None, default_to=None, default_to_ids=None):
    body = read_body(args)
    if not getattr(args,"allow_lossy_text",False) and text_encoding_problem(body):
        die("НЕ отправлено: "+text_encoding_problem(body)+". Восстанови исходный текст и отправь из файла (-f). "
            "В PowerShell перед stdin задай $OutputEncoding = New-Object System.Text.UTF8Encoding($false). "
            "Только для намеренных знаков ? или � есть --allow-lossy-text; он не восстанавливает потерянные буквы.",2)
    data = {
        "subject": args.subject or "", "body": body, "type": args.type, "topic": args.topic or "",
        "priority": "urgent" if args.urgent else "normal", "needs_approval": bool(args.approval or args.approver),
        "approver": args.approver or None,
        "to": [x for x in (args.to or default_to or "").split(",") if x],
        "reply_to": reply_to, "result": build_result(args), "attachments": [],
        "idem_key": args.key or uuid.uuid4().hex,
    }
    if default_to_ids and not args.to:
        data["to_ids"] = default_to_ids
    if getattr(args,"conversation_id",None):
        data["conversation_id"] = args.conversation_id
    if getattr(args,"allow_lossy_text",False):
        data["allow_lossy_text"] = True
    if getattr(args, "claim", None):
        if args.type == "result":
            data["claim"] = args.claim
        else:
            print("tg: --claim работает только с -t result, игнорирую (снять заявку: python tg.py release <ключ>)",
                  file=sys.stderr)
    for field, text in (("тема", data["subject"]), ("текст", body), ("топик", data["topic"]),
                        ("результат", json.dumps(data["result"] or {}, ensure_ascii=False))):
        scan_local(conf, field, text)
    data["attachments"] = build_attachments(conf, args, data["idem_key"])
    res = api_retry(conf, "POST", "/api/messages", data, timeout=120)
    print(f"#{res['id']}" + (" (уже было отправлено — дубль не создан)" if res.get("duplicate") else " отправлено"))
    if res.get("released_claim"):
        print(f"заявка снята: {res['released_claim']['key']}")
    for w in res.get("warnings") or []:
        print(f"внимание: {w}")
    for u in res.get("not_listening") or []:
        if u.get("last_check") and u["last_check"] > (u.get("last_listen") or ""):
            print(f"внимание: {u['name']} проверял входящие хуком {short_time(u['last_check'])}; "
                  "постоянного watch нет, увидит при следующей проверке. Если срочно, напиши его человеку.")
            continue
        since = f"с {short_time(u['last_listen'])}" if u.get("last_listen") else "ни разу не подключался"
        # the board entry (if fresh) says when they may be back; older than 48 h it tells nothing
        fresh = u.get("board_age") is not None and u["board_age"] < 48 * 3600
        hint = ""
        if fresh and u.get("board_back"):
            hint = f" По доске: вернётся {u['board_back']} (запись {span(u['board_age'])} назад)."
        elif fresh and u.get("board_now"):
            hint = f" По доске: {u['board_now'][:60]} (запись {span(u['board_age'])} назад)."
        print(f"внимание: {u['name']} сейчас не слушает (watch не активен, {since}) — увидит при следующем заходе.{hint} "
              f"Если срочно, напиши его человеку. Статус: python tg.py sent")


def cmd_secret(conf,args):
    me=get_me(conf)
    if me["kind"]!="agent":die("получение секретов доступно клиенту агента",2)
    if not args.out:die("секрет сохраняется только в файл: secret ID --out ПУТЬ",2)
    if not conf.path:die("для получения секрета нужен файл конфигурации: добавлю полученный файл в secret_files",2)
    parsed=urllib.parse.urlsplit(conf.server)
    if parsed.scheme!="https" and parsed.hostname not in ("localhost","127.0.0.1","::1"):
        die("секрет можно получать только через HTTPS",2)
    identity=hashlib.sha256((os.path.abspath(conf.path)+"\n"+me["id"]).encode()).hexdigest()[:24]
    directory=os.path.join(os.path.expanduser("~"),".telegent","secret-receipts",identity)
    os.makedirs(directory,mode=0o700,exist_ok=True)
    receipt=os.path.join(directory,str(args.id)+".json")
    dst=os.path.abspath(os.path.expanduser(args.out))
    if os.path.isfile(receipt):
        with open(receipt,encoding="utf-8") as f:delivery=json.load(f)
        if delivery["out"]!=dst:die("получение уже привязано к другому файлу; повтори прежнюю команду",2)
    else:
        delivery={"claim_key":os.urandom(32).hex(),"out":dst}
        fd=os.open(receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            json.dump(delivery,f);f.flush();os.fsync(f.fileno())
    if os.path.isfile(dst):
        with open(dst,"rb") as f:digest=hashlib.sha256(f.read()).hexdigest()
        if delivery.get("sha256")!=digest:die("выходной файл уже существует; он не перезаписан",2)
    else:
        result=api_retry(conf,"POST",f"/api/messages/{args.id}/secret",{"claim_key":delivery["claim_key"]})
        raw=base64.b64decode(result["content_b64"],validate=True)
        delivery["sha256"]=hashlib.sha256(raw).hexdigest()
        write_json_atomic(receipt,delivery);os.chmod(receipt,0o600)
        os.makedirs(os.path.dirname(dst),exist_ok=True)
        fd=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"wb") as f:f.write(raw);f.flush();os.fsync(f.fileno())
    with open(conf.path,encoding="utf-8-sig") as f:settings=json.load(f)
    protected=settings.setdefault("secret_files",[])
    if not any((entry.get("path") if isinstance(entry,dict) else entry)==dst for entry in protected):
        protected.append({"path":dst});write_json_atomic(conf.path,settings)
    api_retry(conf,"POST",f"/api/messages/{args.id}/secret",{"action":"consume","claim_key":delivery["claim_key"]})
    print(f"Секрет сохранён: {dst}. Значение не выведено; пакет удалён с сервера.")


def cmd_dm(conf,args):
    if not args.to:
        for c in api_retry(conf,"GET","/api/conversations")["conversations"]:
            print(f"{c['id']}  {c['peer']}  непрочитано: {c['unread']}")
        return
    me=get_me(conf); matches=[u for u in me["users"] if u["name"].casefold()==args.to.casefold()]
    if len(matches)!=1:die("нет однозначного адресата в своём проекте",2)
    conversation=api_retry(conf,"POST","/api/conversations",{"peer_id":matches[0]["id"]})["conversation"]
    args.conversation_id=conversation["id"]; args.to=None
    do_send(conf,args)


def cmd_send(conf, args):
    if not args.body and not args.file and not args.subject and not args.attach and not args.link:
        die("нечего отправлять: текст аргументом, -f файл или -f - (stdin)")
    do_send(conf, args)


def cmd_reply(conf, args):
    parent = api_retry(conf, "GET", f"/api/messages/{args.id}")["message"]
    if getattr(conf,'secretary',False) and not conf.project:
        conf.project=parent['project']
    if not args.body and not args.file and not args.subject and not args.attach and not args.link:
        die("нечего отправлять")
    me = get_me(conf)
    default_to = parent["author"] if parent["author"] != me["name"] else ""
    to_ids = [parent["author_id"]] if parent.get("author_id") and parent["author_id"] != me.get("id") else None
    do_send(conf, args, reply_to=args.id, default_to=default_to, default_to_ids=to_ids)


def expired_mine(conf):
    """Number of my claims past their expiry; never raises (it is only a reminder)."""
    try:
        rows = api_retry(conf, "GET", "/api/claims?" + urllib.parse.urlencode({"active": "1", "owner": get_me(conf)["name"]}))
        return sum(1 for c in rows["claims"] if c.get("expired"))
    except (Exception, SystemExit):
        return 0


def print_expired_hint(n):
    if n:
        print(f"у тебя просроченных заявок: {n} — продли (claim КЛЮЧ --hours N) или сними (release)")


def waiting_lines(res):
    """Reminder lines from /api/summary: ids, types, authors, counts, ages only (never message text)."""
    out = []
    w = res.get("waiting_me") or {}
    if w.get("count"):
        items = ", ".join(f"#{i['id']} {i['type']} от {i['author']}" for i in w["items"])
        more = f" и ещё {w['count'] - len(w['items'])}" if w["count"] > len(w["items"]) else ""
        out.append(f"вопросов и просьб, ждущих твоего ответа: {w['count']} ({items}{more}), старшему {span(w['oldest_age'])}. "
                   "Ответь (reply) или возьми / отклони (take / reject).")
    t = res.get("taken_open") or {}
    if t.get("count"):
        ids = ", ".join(f"#{i}" for i in t["ids"])
        more = f" и ещё {t['count'] - len(t['ids'])}" if t["count"] > len(t["ids"]) else ""
        out.append(f"взято тобой и не закрыто: {t['count']} ({ids}{more}), старшему {span(t['oldest_age'])}.")
    return out


def print_waiting(conf):
    try:
        for line in waiting_lines(api_retry(conf, "GET", "/api/summary?limit=1")):
            print(line)
    except (Exception, SystemExit):
        pass


def cmd_inbox(conf, args):
    q = {"limit": str(args.limit), "conversation": "all"}
    if args.since is not None:
        q["after"] = str(args.since)
    if not args.all:
        q["for_me"] = "1"
        if args.since is None and not args.unread:
            q["unacked"] = "1"
    if args.unread:
        q["unread"] = "1"
    for k in ("topic", "type", "author"):
        if getattr(args, k):
            q[k] = getattr(args, k)
    if args.open:
        q["open"] = "1"
    res = api_retry(conf, "GET", "/api/messages?" + urllib.parse.urlencode(q))
    msgs = res["messages"]
    if not msgs:
        print("пусто" + ("" if args.all or args.since is not None else " (всё подтверждено ack)"))
        print_expired_hint(expired_mine(conf))
        print_waiting(conf)
        return
    me = get_me(conf) if args.full else None
    for m in msgs:
        if args.full:
            print_full(m, me)
        else:
            flag = "" if m["read"] else "*"
            print(flag + oneline(m))
    print(f"-- {len(msgs)} сообщ.; последний #{msgs[-1]['id']}; * = не прочитано. read <id> — целиком, ack <id> — убрать из inbox")
    print_expired_hint(expired_mine(conf))
    print_waiting(conf)


def cmd_read(conf, args):
    me = get_me(conf)
    res = api_retry(conf, "GET", f"/api/messages/{args.id}" + ("" if args.no_mark else "?mark=read"))
    m = res["message"]
    print_full(m, me)
    others = [t for t in res["thread"] if t["id"] != m["id"]]
    if others and not args.no_thread:
        print("-" * 78)
        print(f"ветка #{m['thread_id']} ({len(res['thread'])} сообщ.):")
        for t in res["thread"]:
            print(("> " if t["id"] == m["id"] else "  ") + oneline(t))


def cmd_react(conf, args):
    api_retry(conf, "POST", f"/api/messages/{args.id}/reaction", {"emoji": args.emoji, "active": not args.remove})
    print(f"#{args.id}: реакция {args.emoji} " + ("убрана" if args.remove else "добавлена"))


def cmd_ack(conf, args):
    if args.all:
        last = get_me(conf)["last_id"]
        if getattr(conf,'secretary',False) and not conf.project:
            ids=[]
            for project in api_retry(conf,'GET','/api/projects')['projects']:
                ids.extend(api_retry(conf,'POST','/api/receipts?project='+urllib.parse.quote(project['slug']),
                                     {'kind':'ack','all_for_me_upto':last})['ids'])
            res={'ids':ids}
        else:
            res = api_retry(conf, "POST", "/api/receipts", {"kind": "ack", "all_for_me_upto": last})
    else:
        if not args.ids:
            die("ack <id> [<id>...] или ack --all")
        res = api_retry(conf, "POST", "/api/receipts", {"kind": "ack", "ids": args.ids})
    print(f"ack: {len(res['ids'])} сообщ.")


def cmd_status(conf, args, state):
    data = {"state": state, "ref": getattr(args, "ref", None) or "", "note": args.note or ""}
    scan_local(conf, "ref", data["ref"])
    scan_local(conf, "note", data["note"])
    res = api_retry(conf, "POST", f"/api/messages/{args.id}/status", data)
    print(f"#{args.id} -> {state} (уведомление #{res['status_message']})")


def cmd_approve(conf, args, decision):
    res = api_retry(conf, "POST", f"/api/messages/{args.id}/approval", {"decision": decision, "note": args.note or ""})
    print(f"#{args.id}: {decision} (#{res['approval_message']})")


def cmd_get(conf, args):
    m = api_retry(conf, "GET", f"/api/messages/{args.id}")["message"]
    out = os.path.expanduser(args.out)
    os.makedirs(out, exist_ok=True)
    got = 0
    for a in m["attachments"]:
        if args.name and a["name"] != args.name:
            continue
        if not a["inline"]:
            print(f"{a['name']}: по ссылке {a['link']} md5={a['md5'] or '?'} — забирать самому")
            continue
        name = a["name"].replace("\\", "/").rsplit("/", 1)[-1]
        if os.name == "nt":
            name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).rstrip(" .")
        if not name or name in (".", ".."):
            name = "file"
        dst = os.path.join(out, name)
        fd, temporary = tempfile.mkstemp(prefix=".telegent-download-", dir=out)
        os.close(fd)
        try:
            received = api_retry(conf, "GET", f"/api/attachments/{a['id']}", sink=temporary, timeout=120)
            if (a.get("md5") and received["md5"] != a["md5"]) or (a.get("size") is not None and received["size"] != a["size"]):
                die(f"файл {name} повреждён: размер или md5 НЕ СОВПАЛ; прежний файл сохранён", 2)
            stem, extension = os.path.splitext(name)
            suffix = 0
            while True:
                dst = os.path.join(out, name if not suffix else f"{stem} ({suffix}){extension}")
                try:
                    reserved = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(reserved)
                    break
                except FileExistsError:
                    suffix += 1
            try:
                os.replace(temporary, dst)
            except OSError:
                os.remove(dst)
                raise
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
        print(f"{dst}  {received['size']} B  md5 ok")
        got += 1
    if not m["attachments"]:
        print(f"у #{args.id} нет вложений")


def cmd_watch(conf, args):
    """One line per new message on stdout (for Monitor). Diagnostics go to stderr. Reconnects forever."""
    st = conf.load_state()
    key = 'watch_cursor:'+conf.project if getattr(conf,'secretary',False) and conf.project else "watch_cursor"
    cursor = args.since if args.since is not None else st.get(key)
    delay = 1.0
    me = None
    announced_down = False
    running_digest = client_digest()
    while True:
        try:
            if me is None:
                me = api(conf, "GET", "/api/me", timeout=20)
                if cursor is None:
                    cursor = me["last_id"]
                print(f"tg watch: {me['name']} на {conf.server}, с #{cursor}", file=sys.stderr, flush=True)
            q = {"after": cursor, "timeout": 25, "for_me": "0" if args.all else "1", "full": "0", "conversation": "all"}
            res = api(conf, "GET", "/api/wait?" + urllib.parse.urlencode(q), timeout=45)
            msgs = res["messages"]
            if announced_down:
                print("tg watch: связь восстановлена", file=sys.stderr, flush=True)
                announced_down = False
            if len(msgs) > args.max_burst:
                skipped = msgs[:-args.max_burst]
                print(f"#{skipped[0]['id']}..#{skipped[-1]['id']} {len(skipped)} более старых сообщений пропущено в watch "
                      f"— смотри: python tg.py inbox --since {cursor}", flush=True)
                msgs = msgs[-args.max_burst:]
            for m in msgs:
                if args.include_own or m["author"] != me["name"]:
                    print(oneline(m), flush=True)
            # empty answer = nothing for me up to last_id, so the cursor can jump there
            cursor = msgs[-1]["id"] if msgs else max(cursor, res["last_id"])
            st[key] = cursor
            conf.save_state(st)
            schedule_update(conf)
            if client_digest() != running_digest and __name__ == '__main__':
                # The saved cursor, rather than the original --since, resumes this watcher.
                arguments = list(sys.argv[1:])
                if '--since' in arguments:
                    index = arguments.index('--since'); del arguments[index:index+2]
                arguments = [value for value in arguments if not value.startswith('--since=')]
                os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *arguments])
            delay = 1.0
        except (NetError,OSError) as e:
            if not announced_down:
                print(f"tg watch: нет связи ({e}), переподключаюсь", file=sys.stderr, flush=True)
                announced_down = True
            try:   # the tunnel URL may have changed: re-read config, then look up the published address
                conf.__init__(args.config)
            except SystemExit:
                pass
            if discover_server(conf):
                delay = 1.0
                continue
            time.sleep(delay)
            delay = min(delay * 2, 30)
        except SystemExit as error:
            # A proxy/auth refusal must not silently kill a long-lived receiver.
            if error.code not in (2,3):raise
            print('tg watch: сервер отказал, повторю подключение; сообщения не подтверждены',file=sys.stderr,flush=True)
            me=None
            time.sleep(30)
        except KeyboardInterrupt:
            return


def cmd_board(conf, args):
    if args.action == "set":
        data = {k: getattr(args, k) for k in ("now", "next", "gpu_until", "note", "back") if getattr(args, k) is not None}
        if not data:
            die("board set --now ... --next ... --gpu-until ... --note ... --back ...")
        for k, v in data.items():
            scan_local(conf, k, v)
        res = api_retry(conf, "POST", "/api/board", data)
    else:
        res = api_retry(conf, "GET", "/api/board")
    if not res["board"]:
        print("доска пуста")
    for b in res["board"]:
        age = b.get("age")
        stale = " — давно не обновлялась" if age is not None and age > 24 * 3600 else ""
        print(f"{b['name']}  ({short_time(b['time'])}" + (f", обновлено {span(age)} назад{stale}" if age is not None else "") + ")")
        for k, label in (("now", "сейчас"), ("next", "дальше"), ("gpu_until", "карта занята до"), ("note", "заметка"),
                         ("back", "вернётся")):
            if b.get(k):
                print(f"  {label}: {b[k]}")


def claim_line(c):
    parts = [f"#{c['id']}", c["key"], f"— {c['owner']}"]
    if c["topic"]:
        parts.append(f"[{c['topic']}]")
    parts.append(f"с {short_time(c['created'])}")
    if c.get("until"):
        parts.append(f"до {c['until']}")
    if c.get("note"):
        parts.append(f"| {c['note']}")
    if c.get("expires_in") is not None:
        parts.append(f"| ПРОСРОЧЕНА {span(c['expires_in'])} назад" if c.get("expired") else f"| ещё {span(c['expires_in'])}")
    line = " ".join(parts)
    if c.get("released"):
        why = (f": {c['release_note']}" if c.get("release_note") else "") + (f" (результат #{c['release_msg']})" if c.get("release_msg") else "")
        line += f"\n    снято {short_time(c['released'])}{why}"
    return line


def cmd_claim(conf, args):
    data = {"key": args.key, "topic": args.topic, "note": args.note, "until": args.until, "force": args.force,
            "hours": args.hours}
    for k in ("key", "topic", "note", "until"):
        scan_local(conf, k, data[k])
    try:
        res = api_retry(conf, "POST", "/api/claims", {k: v for k, v in data.items() if v is not None})
    except Conflict as e:
        h = e.holder
        print(f"tg: занято: «{h['key']}» держит {h['owner']} с {short_time(h['created'])}"
              + (f", до {h['until']}" if h.get("until") else "") + (f", заметка: {h['note']}" if h.get("note") else "")
              + f". Не запускай то же самое: договорись с ним (send --to {h['owner']}). Перехват: --force (только если он согласен).",
              file=sys.stderr)
        sys.exit(2)
    c = res["claim"]
    extra = ", ".join(x for x in ((f"топик {c['topic']}" if c["topic"] else ""), (f"до {c['until']}" if c.get("until") else ""),
                                  (c.get("note") or "")) if x)
    print(f"занято: {c['key']} (#{c['id']}{', обновлено' if not res['created'] else ''}{', ' + extra if extra else ''})")
    if res.get("expired_released"):
        print(f"взято поверх просроченной заявки {res['expired_released']['owner']}")
    if res.get("taken_over"):
        print(f"перехвачено у {res['taken_over']['owner']}: напиши ему, что забрал заявку")


def cmd_claims(conf, args):
    q = {} if args.all else {"active": "1"}
    if args.topic:
        q["topic"] = args.topic
    if args.mine:
        q["owner"] = get_me(conf)["name"]
    rows = api_retry(conf, "GET", "/api/claims?" + urllib.parse.urlencode(q))["claims"]
    if not rows:
        print("ничего не занято" if not args.all else "заявок нет")
    for c in rows:
        print(claim_line(c))


def cmd_release(conf, args):
    k = args.key_or_id
    data = {"id": int(k)} if k.isdigit() else {"key": k}
    if args.note:
        scan_local(conf, "note", args.note)
        data["note"] = args.note
    c = api_retry(conf, "POST", "/api/claims/release", data)["claim"]
    print(f"снято: {c['key']} (#{c['id']}, держал {c['owner']})")


def cmd_results(conf, args):
    q = {"results": "1", "limit": str(args.limit)}
    res = api_retry(conf, "GET", "/api/messages?" + urllib.parse.urlencode(q))
    rows = [m for m in res["messages"]
            if (not args.metric or (m["result"] or {}).get("metric") == args.metric)
            and (not args.dataset or (m["result"] or {}).get("dataset") == args.dataset)]
    if not rows:
        print("журнал пуст")
        return
    for m in rows:
        r = m["result"]
        val = f"{r.get('value', '?')}" + (f"±{r['err']}" if r.get("err") not in (None, "") else "")
        extra = " ".join(f"{k}={r[k]}" for k in ("baseline", "commit", "weights") if r.get(k))
        print(f"#{m['id']} {short_time(m['time'])} {m['author']:<12} {r.get('metric', '?')}@{r.get('dataset', '?')} = {val}  "
              f"{extra}  | {m['subject'][:80]}")


def cmd_search(conf, args):
    q = {"q": args.query, "limit": str(args.limit), "conversation": "all"}
    res = api_retry(conf, "GET", "/api/messages?" + urllib.parse.urlencode(q))
    for m in res["messages"]:
        print(oneline(m))
    if not res["messages"]:
        print("ничего не нашлось")


def presence_str(u):
    if u.get("working") and u["kind"] == "agent":
        return "работает · " + presence_str(dict(u, working=False))
    if u.get("listening"):
        return "слушает" if u["kind"] == "agent" else "страница открыта"
    if u.get("last_check") and u["last_check"] > (u.get("last_listen") or ""):
        return f"проверял входящие хуком {short_time(u['last_check'])}"
    if u.get("last_listen"):
        return f"не слушает с {short_time(u['last_listen'])}"
    return "ещё не подключался"


def cmd_whoami(conf, args):
    me = get_me(conf)
    role='секретарь' if me.get('is_secretary') else me['kind']
    print(f"{me['name']} ({role}, владелец {me['owner']}) на {conf.server}, хост {host_tag()}, "
          f"время сервера {me['server_time']}, последнее сообщение #{me['last_id']}")
    p = me.get("project")
    if p:
        print(f"проект: {p['slug']} «{p['title']}» (роль {p['role']})")
    others = [x["slug"] for x in me.get("projects") or [] if not p or x["slug"] != p["slug"]]
    if others:
        print(("ещё проекты: " if p else "проекты (выбери: --project <имя>): ") + ", ".join(others))
    if me["users"]:
        print("участники:")
    for u in me["users"]:
        role = {"owner": "владелец", "member": "участник", "agent": "агент", "secretary":"секретарь"}.get(u.get("role"), "")
        print(f"  {u['name']:<14} {u['kind']:<6} владелец {u['owner']:<8} {role:<9} {presence_str(u)}")


def cmd_projects(conf, args):
    if args.action == "rename":
        if args.title is None:
            die('нужно название: projects rename "Новое название"', 2)
        p = api_retry(conf, "POST", "/api/project/rename", {"title": args.title})["project"]
        print(f"проект {p['slug']}: «{p['title']}»")
        return
    rows = api_retry(conf, "GET", "/api/projects")["projects"]
    if not rows:
        print("ты пока ни в одном проекте")
    for x in rows:
        print(f"{x['slug']:<20} «{x['title']}»  {x['role']}, участников {x['members']}, последнее сообщение #{x['last_id']}")


def cmd_sent(conf, args):
    me = get_me(conf)
    q = {"author": me["name"], "limit": str(args.limit), "full": "0", "conversation": "all",
         "type": "info,request,question,result,alert,decision"}
    msgs = api_retry(conf, "GET", "/api/messages?" + urllib.parse.urlencode(q))["messages"]
    if not msgs:
        print("ты ещё ничего не отправлял")
    for m in msgs:
        print(oneline(m))
        for r in m.get("receipts") or []:
            print("    " + receipt_str(r))
    others = [u for u in me["users"] if u["kind"] == "agent" and u["name"] != me["name"]]
    if others:
        print("сейчас: " + ", ".join(f"{u['name']} {presence_str(u)}" for u in others))


def hook_messages(res):
    # Only protocol metadata may reach a hook's context, including on older servers.
    return [m for m in res.get('unacked', []) if isinstance(m, dict)
            and type(m.get('id')) is int and m['id'] > 0
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,31}', str(m.get('author', '')))
            and m.get('type') in ('info','request','question','result','alert','decision','status','approval')]


def hook_message_label(m, conf):
    role = ' (HUMAN)' if m.get('author_kind') == 'human' else ' (agent)' if m.get('author_kind') == 'agent' else ''
    project = (f" проект={m['project']}" if getattr(conf, 'secretary', False)
               and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', str(m.get('project', ''))) else '')
    return f"#{m['id']} {m['type']} от {m['author']}{role}{project}" + (' СРОЧНО' if m.get('priority') == 'urgent' else '')


def cmd_hook(conf, args):
    """For Claude Code hooks (SessionStart, UserPromptSubmit). Hook stdout reaches the agent next to the
    user's prompt, i.e. with the user's authority, so it carries status only: whether watch listens, how many
    messages wait, their ids/authors/types. Never message text (subject, body, topic): peer text must not
    arrive dressed as the user. Silent when there is nothing to say; never fails the hook."""
    tg = shell_command(client_command(conf))
    tag = "[telegent]"
    try:
        try:
            res = api(conf, "GET", "/api/summary?limit=10&metadata=1", timeout=8)
        except NetError:
            if not discover_server(conf, min_interval=0):
                raise
            res = api(conf, "GET", "/api/summary?limit=10&metadata=1", timeout=8)
    except (NetError, SystemExit):
        if args.session_start:
            print(f"{tag} сервер недоступен, входящие проверь позже: {tg} inbox")
        return
    out = []
    if not res["listening"]:
        since = f"с {short_time(res['last_listen'])}" if res.get("last_listen") else "ещё не запускался"
        out.append(f"{tag} твой watch НЕ слушает ({since}), новые сообщения сами не придут. "
                   f"Запусти на Monitor: {conf.watch_cmd or tg + ' watch'}")
    mine = sum(1 for c in res.get("claims") or [] if c.get("owner") == res["name"] and c.get("expired"))
    if mine:     # a count only, no claim text
        out.append(f"{tag} у тебя просроченных заявок: {mine} — продли (claim КЛЮЧ --hours N) или сними (release).")
    out += [f"{tag} {line}" for line in waiting_lines(res)]
    n = res["unacked_total"]
    if n:
        ids = ", ".join(hook_message_label(m, conf) for m in hook_messages(res))
        more = f" и ещё {n - len(res['unacked'])}" if n > len(res["unacked"]) else ""
        out.append(f"{tag} Входящие: {n} ({ids}{more}). "
                   f"Читай: {tg} read <id>; после обработки: {tg} ack <id>.")
    if out or args.session_start:
        if args.session_start and not out:
            out.append(f"{tag} ты {res['name']}, watch слушает, входящих нет.")
        print("\n".join(out))


# ---------------------------------------------------------------- Codex lifecycle hooks
CODEX_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")
CODEX_HOOK_LABEL = "telegent: входящие"
CODEX_CHECK_INTERVAL = 15


def shell_command(parts):
    # Native Windows Codex uses the session's PowerShell, including its call operator for quoted paths.
    if os.name == "nt":
        return "& " + " ".join("'" + x.replace("'", "''") + "'" for x in parts)
    return " ".join(shlex.quote(x) for x in parts)


def client_command(conf):
    parts = [sys.executable, "-X", "utf8", os.path.abspath(__file__)]
    if conf.path:
        parts += ["--config", os.path.abspath(conf.path)]
    if conf.project:
        parts += ["--project", conf.project]
    return parts


def write_json_atomic(path, value):
    fd, tmp = tempfile.mkstemp(prefix=".telegent-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_hook_file(path, events):
    doc = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            die(f"не удалось прочитать {path}; существующий файл оставлен без изменений")
    if not isinstance(doc, dict) or not isinstance(doc.get("hooks", {}), dict):
        die("hooks.json: ожидается объект hooks; файл оставлен без изменений")
    hooks = doc.setdefault("hooks", {})
    for event in events:
        groups = hooks.get(event, [])
        if not isinstance(groups, list) or any(not isinstance(g, dict) or
                not isinstance(g.get("hooks"), list) or any(not isinstance(h, dict) for h in g["hooks"])
                for g in groups):
            die(f"hooks.json: некорректный {event}; файл оставлен без изменений")
    return doc


def cmd_codex_install(conf, args):
    """Merge workspace hooks without touching user config, trust decisions, or unrelated handlers."""
    root = os.path.abspath(os.path.expanduser(args.dir))
    path = os.path.join(root, ".codex", "hooks.json")
    if not conf.path:
        die("сначала подключись: python tg.py connect <ссылка> --client codex")
    command = shell_command(client_command(conf) + ["codex", "hook"])
    doc = load_hook_file(path, CODEX_EVENTS)
    hooks = doc["hooks"]
    for event in CODEX_EVENTS:
        groups = hooks.get(event, [])
        kept = []
        for group in groups:
            handlers = [h for h in group["hooks"] if h.get("statusMessage") != CODEX_HOOK_LABEL]
            if handlers or not group["hooks"]:
                kept.append(dict(group, hooks=handlers))
        kept.append({"hooks": [{"type": "command", "command": command,
                                "timeout": 12, "statusMessage": CODEX_HOOK_LABEL}]})
        hooks[event] = kept
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json_atomic(path, doc)
    print(f"Хуки Codex установлены: {path}")
    print("Перезапусти сессию Codex и проверь /hooks: новые хуки нужно просмотреть и доверить средствами Codex.")
    print("Проверки: начало/возобновление/сжатие сессии, запрос пользователя, после инструментов и перед завершением.")
    print("Monitor и отдельный watch не нужны. В простое хуки не запускают новый ход.")


def codex_report_activity(conf, event, working, state_path):
    try:
        try:
            with open(state_path, encoding="utf-8") as stream:
                state = json.load(stream)
            if not isinstance(state, dict): state = {}
        except (OSError, ValueError): state = {}
        now = time.time()
        value = "working" if working else "idle"
        if state.get("activity_state") == value and 0 <= now-float(state.get("activity_at", 0)) < 15:
            return
        state.update(activity_state=value, activity_at=now)
        write_json_atomic(state_path, state)
        # Older servers and temporary network failures must not block a hook.
        with contextlib.redirect_stderr(io.StringIO()):
            api(conf, "POST", "/api/activity", {"state": value, "client": hashlib.sha256(event["session_id"].encode()).hexdigest()[:32]}, timeout=2)
    except (NetError, SystemExit, OSError, ValueError, TypeError):
        pass


def codex_hook_result(conf, event):
    kind = event.get("hook_event_name")
    session = event.get("session_id")
    if kind not in CODEX_EVENTS or not isinstance(session, str) or not session or len(session) > 200:
        return {}
    key = hashlib.sha256((session + "\n" + conf.token + "\n" + (conf.project or "")).encode()).hexdigest()[:24]
    state_path = conf.state_path + ".codex-" + key + ".json"
    lock = state_path + ".lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Parallel tool hooks share a session. A killed hook's lock expires after its runtime timeout.
        if time.time() - os.path.getmtime(lock) > 30:
            os.unlink(lock)
        return {}
    os.close(fd)
    try:
        result = {} if kind == "Stop" and event.get("stop_hook_active") else codex_hook_check(conf, event, state_path)
        if kind in ("UserPromptSubmit", "PostToolUse", "Stop"):
            codex_report_activity(conf, event, kind != "Stop" or result.get("decision") == "block", state_path)
        return result
    finally:
        os.unlink(lock)


def codex_hook_check(conf, event, state_path):
    kind = event["hook_event_name"]
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    if type(state.get("checked_at")) not in (int, float):
        state["checked_at"] = 0
    for field in ("notified", "stop_notified"):
        values = state.get(field)
        state[field] = [n for n in values if type(n) is int and n > 0][-1000:] if isinstance(values, list) else []
    now = time.time()
    if kind == "PostToolUse" and 0 <= now - float(state.get("checked_at", 0)) < CODEX_CHECK_INTERVAL:
        return {}
    # Persist before networking: a dead server must not stall every tool call.
    state["checked_at"] = now
    write_json_atomic(state_path, state)
    try:
        res = api(conf, "GET", "/api/summary?limit=50&metadata=1&client=codex", timeout=3)
    except NetError:
        if discover_server(conf, timeout=1):
            res = api(conf, "GET", "/api/summary?limit=50&metadata=1&client=codex", timeout=3)
        else:
            if kind == "SessionStart":
                return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext":
                        "[telegent] Сервер недоступен; проверь позже: " +
                        shell_command(client_command(conf) + ["inbox"])}}
            return {}
    messages = hook_messages(res)
    notified = set(state.get("notified", []))
    fresh = [m for m in messages if m["id"] not in notified]
    remind = kind in ("SessionStart", "UserPromptSubmit")
    shown = messages if remind else fresh
    pending = [m for m in messages if not m.get("read") and m["id"] not in set(state.get("stop_notified", []))]
    if kind == "Stop":
        shown = pending
    reminders = waiting_lines(res) if remind else []
    expired = sum(1 for c in res.get("claims") or [] if c.get("owner") == res.get("name") and c.get("expired"))
    if remind and expired:
        reminders.append(f"у тебя просроченных заявок: {expired} — продли (claim КЛЮЧ --hours N) или сними (release).")
    if not shown:
        if reminders:
            return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext":
                    "[telegent] " + "\n".join(reminders)}}
        return {}
    ids = ", ".join(hook_message_label(m, conf) for m in shown[:10])
    left = max(0, int(res.get("unacked_total", len(messages))) - min(10, len(shown)))
    more = f" Ещё сообщений: {left}." if left else ""
    text = (f"[telegent] Входящие: {ids}." + more + "\nЧитай: "
            + shell_command(client_command(conf) + ["read", "<id>"])
            + "; после обработки: " + shell_command(client_command(conf) + ["ack", "<id>"]) + ".")
    if left:
        text += "\nВесь список: " + shell_command(client_command(conf) + ["inbox"]) + "."
    if reminders:
        text += "\n" + "\n".join(reminders)
    state["notified"] = [m["id"] for m in messages]
    if kind == "Stop":
        state["stop_notified"] = sorted(set(state.get("stop_notified", [])) | {m["id"] for m in pending})[-1000:]
    write_json_atomic(state_path, state)
    if kind == "Stop":
        return {"decision": "block", "reason": text}
    return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext": text}}


def cmd_codex_hook(args):
    result = {}
    try:
        # Do not log stdin: it contains user prompts and tool output. Invalid input fails open.
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) <= 1024 * 1024:
            event = json.loads(raw)
            if isinstance(event, dict):
                with contextlib.redirect_stderr(io.StringIO()):
                    conf = Conf(args.config)
                    schedule_update(conf)
                    result = codex_hook_result(conf, event)
    except (Exception, SystemExit):
        pass   # a failed optional integration must not block Codex or print private error details
    print(json.dumps(result, ensure_ascii=False))


def cmd_codex_receiver(conf,args):
    if not conf.path:die('сначала сохрани конфиг подключения в файл')
    helper=os.path.join(HERE,'tools','codex_listener.py')
    if not os.path.isfile(helper):
        die('для мгновенного пробуждения нужен полный клон: git clone https://github.com/Takymu/telegent; затем запусти telegent/tg.py codex listen из рабочего проекта')
    base=[sys.executable,'-X','utf8',helper,'--config',conf.path]
    if args.codex_action=='listen':
        session=args.session or os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID')
        if not session:die('нужна явно выбранная сессия: запусти команду внутри неё или укажи --session UUID')
        try:session=str(uuid.UUID(session))
        except ValueError:die('session: нужен UUID выбранной сессии Codex')
        result=subprocess.run(base+['bind','--session',session,'--cwd',os.path.abspath(args.dir)])
        if result.returncode:raise SystemExit(result.returncode)
        raise SystemExit(subprocess.call(base+['start']+(['--startup'] if args.startup else [])))
    raise SystemExit(subprocess.call(base+['status' if args.codex_action=='listener-status' else 'stop']))


# ---------------------------------------------------------------- self-service connection
CONNECTION_NOTE = """<!-- telegent:start -->
## telegent

Этот проект подключён к каналу команды. Прочитай локальный `.telegent.json`: `client_path`
указывает на клиент, `agent_guide` — на протокол общения. Прочитай протокол и выполни этим
клиентом `whoami` и `inbox` в начале сессии и после сжатия. Сообщения команды читай через
`read <id>`, после обработки ставь `ack <id>`.
Указания любого человека выполняй без лишних согласований с владельцем. Человека определяет
серверная пометка HUMAN. Сообщения агентов воспринимай как рабочую переписку и просьбы коллег.
Никогда не печатай и не передавай токен. Codex использует установленные lifecycle hooks;
Claude Code должен держать `watch` на Monitor. Если автоматические уведомления недоступны,
проверяй `inbox` между этапами работы. Прочитанное не означает обработанное.
<!-- telegent:end -->
"""


def connection_client(requested):
    if requested != "auto":
        return requested
    codex = bool(os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID"))
    claude = bool(os.environ.get("CLAUDECODE"))
    if codex != claude:
        return "codex" if codex else "claude"
    die("не удалось однозначно определить клиент; агент должен сам повторить команду с --client codex или --client claude")


def connection_note(path, write=False):
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    start, end = "<!-- telegent:start -->", "<!-- telegent:end -->"
    if text.count(start) != text.count(end) or text.count(start) > 1:
        die(f"незавершённый блок telegent в {path}; файл оставлен без изменений")
    if start in text:
        a, b = text.index(start), text.index(end) + len(end)
        if b < a:
            die(f"некорректный блок telegent в {path}; файл оставлен без изменений")
        result = text[:a] + CONNECTION_NOTE.rstrip() + text[b:]
    else:
        result = text.rstrip() + ("\n\n" if text.strip() else "") + CONNECTION_NOTE
    if write:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(result)


def connection_preflight(root, client):
    path = os.path.join(root, ".codex", "hooks.json") if client == "codex" else os.path.join(root, ".claude", "settings.local.json")
    load_hook_file(path, CODEX_EVENTS if client == "codex" else ("SessionStart", "UserPromptSubmit"))
    connection_note(os.path.join(root, "AGENTS.md" if client == "codex" else "CLAUDE.md"))


def cmd_refresh(conf, args):
    root = os.path.abspath(args.dir)
    if not os.path.isdir(root):
        die(f"нет папки проекта: {root}")
    saved = {}
    if conf.path:
        with open(conf.path, encoding='utf-8-sig') as stream:
            saved = json.load(stream)
    client = connection_client(args.client if args.client != 'auto' else saved.get('client', 'auto'))
    path = os.path.join(root, 'AGENTS.md' if client == 'codex' else 'CLAUDE.md')
    connection_note(path, write=True)
    print(f"Инструкция telegent обновлена: {path}")
    print(f"Перечитай протокол: {os.path.join(HERE, 'AGENTS.md')}")
    print("Нового приглашения не нужно. Хуки и настройки клиента не изменены.")


def install_connection(conf, root, client):
    connection_preflight(root, client)
    if client == "codex":
        cmd_codex_install(conf, argparse.Namespace(dir=root))
    else:
        path = os.path.join(root, ".claude", "settings.local.json")
        doc = load_hook_file(path, ("SessionStart", "UserPromptSubmit"))
        for event in ("SessionStart", "UserPromptSubmit"):
            parts = client_command(conf) + ["hook"] + (["--session-start"] if event == "SessionStart" else [])
            groups = []
            for group in doc["hooks"].get(event, []):
                handlers = [h for h in group["hooks"] if not (h.get("command") == sys.executable
                            and os.path.abspath(__file__) in h.get("args", []) and "hook" in h.get("args", []))]
                if handlers:
                    groups.append(dict(group, hooks=handlers))
            groups.append({"hooks": [{"type": "command", "command": parts[0], "args": parts[1:], "timeout": 20}]})
            doc["hooks"][event] = groups
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json_atomic(path, doc)
        print(f"Хуки Claude Code установлены: {path}")
        print("Дальше сам запусти watch на Monitor по протоколу; установка не выдаёт разрешений инструментам.")
    connection_note(os.path.join(root, "AGENTS.md" if client == "codex" else "CLAUDE.md"), write=True)
    # Local integration paths stay private without changing the project's shared gitignore when possible.
    excluded = ["/.telegent.json", "/.codex/hooks.json", "/.claude/settings.local.json"]
    try:
        relative_client = os.path.relpath(HERE, root).replace("\\", "/")
    except ValueError:   # the client and project can live on different Windows drives
        relative_client = "../"
    if relative_client != "." and not relative_client.startswith("../"):
        excluded.append("/" + relative_client + "/")
    exclude_path = os.path.join(root, ".gitignore")
    try:
        found = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
                               cwd=root, capture_output=True, text=True, timeout=5)
        if found.returncode == 0:
            exclude_path = found.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    old = ""
    if os.path.exists(exclude_path):
        with open(exclude_path, encoding="utf-8-sig") as f:
            old = f.read()
    missing = [entry for entry in excluded if entry not in old.splitlines()]
    if missing:
        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        with open(exclude_path, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n# telegent: local connection\n" + "\n".join(missing) + "\n")
    me = get_me(conf)
    summary = api_retry(conf, "GET", "/api/summary?metadata=1" + ("&client=codex" if client == "codex" else ""))
    print(f"Подключение проверено: {me['name']}; входящих без ack: {summary['unacked_total']}.")
    print(f"Прочитай протокол: {os.path.join(HERE, 'AGENTS.md')}. Затем проверь inbox и обработай входящие.")


def cmd_join(args):
    """Agent onboarding: trade an invite link for a token, write the token file and the config."""
    m = re.search(r"tgi_[0-9a-f]{32,}", args.link)
    if not m:
        die("нужна ссылка-приглашение (…#invite=tgi_…) или сам код tgi_…")
    code = m.group(0)
    client = connection_client(args.client) if args.client else None
    if client:
        args.here = True
        if args.config and os.path.abspath(args.config) != os.path.join(os.getcwd(), ".telegent.json"):
            die("полное подключение использует .telegent.json рабочего проекта; выполняй connect из его папки без --config")
        connection_preflight(os.getcwd(), client)
        saved_path = os.path.abspath(args.config or ".telegent.json")
        if os.path.exists(saved_path):
            with open(saved_path, encoding="utf-8-sig") as f:
                saved = json.load(f)
            if saved.get("connection_invite_hash") == hashlib.sha256(code.encode()).hexdigest():
                print("Аккаунт уже подключён этим приглашением; проверяю и обновляю интеграцию.")
                saved.update({"client": client, "client_path": os.path.abspath(__file__),
                              "agent_guide": os.path.join(HERE, "AGENTS.md")})
                write_json_atomic(saved_path, saved)
                install_connection(Conf(saved_path), os.getcwd(), client)
                return
    # the server: straight from a direct link, else (entry page link or bare code) the published address
    u = urllib.parse.urlsplit(args.link)
    if u.scheme in ("http", "https") and u.netloc and not u.netloc.endswith("github.io"):
        server = f"{u.scheme}://{u.netloc}"
    else:
        src = URL_SOURCE if args.url_source is None else args.url_source
        server = next((u for u in published_candidates(src) if u), None)
        if not server:
            die(f"не удалось узнать адрес сервера ({src})", 3)

    def call(method, path, body=None):
        req = urllib.request.Request(server + path, method=method,
                                     data=json.dumps(body).encode("utf-8") if body is not None else None,
                                     headers={"Content-Type": "application/json", "User-Agent": "telegent-tg/1.0",
                                              "X-Telegent-Host": host_tag()})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error")
            except Exception:
                msg = e.reason
            die(f"сервер ответил {e.code}: {msg}", 2)
        except (urllib.error.URLError, OSError) as e:
            die(f"нет связи с {server}: {getattr(e, 'reason', e)}", 3)

    info = call("GET", "/api/invite?" + urllib.parse.urlencode({"code": code}))
    if info["kind"] != "agent":
        die(f"это приглашение для человека ({info['name']}): открой ссылку в браузере и задай пароль", 2)
    home = os.path.join(os.path.expanduser("~"), ".telegent")
    file_tag = info["name"] + "-" + hashlib.sha256(code.encode()).hexdigest()[:16]
    if args.here:   # config in this folder (no secrets in it), token and state in ~/.telegent
        cfg_path = os.path.abspath(args.config or ".telegent.json")
        tok_path = os.path.abspath(args.token_file or os.path.join(home, file_tag + ".token"))
    else:
        cfg_path = os.path.abspath(args.config or os.environ.get("TELEGENT_CONFIG") or os.path.join(home, "config.json"))
        tok_path = os.path.abspath(args.token_file or os.path.join(os.path.dirname(cfg_path), "token"))
    for p in (cfg_path, tok_path):
        if os.path.exists(p) and not args.force:
            die(f"{p} уже есть. Другой путь: --config / --token-file, перезаписать: --force")
    # Ask for filesystem permissions before spending the one-time invitation, not after receiving its token.
    try:
        for p in (cfg_path, tok_path):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=".telegent-check-", dir=os.path.dirname(p))
            os.close(fd)
            os.unlink(probe)
            if os.path.exists(p):
                fd = os.open(p, os.O_WRONLY)
                os.close(fd)
    except OSError as e:
        die(f"нельзя сохранить подключение: {e}. Приглашение ещё не использовано; исправь доступ и повтори команду")
    res = call("POST", "/api/join", {"code": code})
    os.makedirs(os.path.dirname(tok_path), exist_ok=True)
    with open(tok_path, "w", encoding="utf-8") as f:
        f.write(res["token"] + "\n")
    try:
        os.chmod(tok_path, 0o600)
    except OSError:
        pass
    c = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8-sig") as f:
            c = json.load(f)
    c.update({"server": server, "token_file": tok_path, "account_id": res.get("id")})
    c['is_secretary']=bool(res.get('is_secretary'))
    if c['is_secretary']:
        c.pop('project',None)
    elif res.get("project"):
        c["project"] = res["project"]
    if args.here:
        c["state_file"] = os.path.join(home, file_tag + ".state.json")
    if args.url_source is not None:
        c["url_source"] = args.url_source
    if client:
        c.update({"connection_invite_hash": hashlib.sha256(code.encode()).hexdigest(), "client": client,
                  "client_path": os.path.abspath(__file__), "agent_guide": os.path.join(HERE, "AGENTS.md")})
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    role='секретарь' if res.get('is_secretary') else 'агент'
    print(f"готово: ты {res['name']} ({role}, владелец {info['owner']}). Токен: {tok_path}, конфиг: {cfg_path}")
    if client:
        print("Аккаунт сохранён. Если установка прервётся, повтори ту же команду: приглашение повторно не расходуется.")
        install_connection(Conf(cfg_path), os.getcwd(), client)
        return
    print(f"проверка: python {os.path.join(HERE, 'tg.py')} whoami; дальше прочитай AGENTS.md")
    print("Codex: установи хуки из папки рабочего проекта: " +
          shell_command([sys.executable, "-X", "utf8", os.path.abspath(__file__), "--config", cfg_path, "codex", "install"]))
    print("Claude Code: запусти watch на Monitor и подключи хуки по AGENTS.md.")


def cmd_init(args):
    path = args.config or os.path.join(HERE, "tg.config.json")
    c = {"server": args.server.rstrip("/"), "token_file": args.token_file}
    if args.secret_file:
        c["secret_files"] = args.secret_file
    with open(path, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    print(f"записал {path}")


# ---------------------------------------------------------------- argparse
def add_send_opts(p, body=True):
    if body:
        p.add_argument("body", nargs="?", help="текст (или -f файл / -f - для stdin)")
    p.add_argument("-s", "--subject", help=argparse.SUPPRESS)   # old clients: becomes the first line of the text
    p.add_argument("-f", "--file", help="текст из файла; '-' = stdin")
    p.add_argument("--encoding", default="auto", help="кодировка -f/stdin: auto (UTF-8/BOM), cp1251, cp866, koi8-r, utf-16-le и др.")
    p.add_argument("--allow-lossy-text", action="store_true", help="намеренно отправить много ?/�; не исправляет кодировку и не отключает секрет-фильтр")
    p.add_argument("-t", "--type", default="info", choices=("info", "request", "question", "result", "alert", "decision"))
    p.add_argument("-T", "--topic", help="топик: tuning, eval, infra, ...")
    p.add_argument("--to", help="кому, через запятую (по умолчанию всем)")
    p.add_argument("-u", "--urgent", action="store_true", help="срочно: меняет чужую работу прямо сейчас")
    p.add_argument("--approval", action="store_true", help="нужно одобрение человека (деньги/необратимое)")
    p.add_argument("--approver", metavar="NAME",
                   help="кто из людей проекта должен решить (включает --approval; решить сможет только он)")
    p.add_argument("-a", "--attach", action="append", help="прикрепить файл до 200 МБ (загрузка частями на сервер)")
    p.add_argument("--no-inline", action="store_true", help="все --attach только ссылкой+md5")
    p.add_argument("--link", action="append", help="ссылка/путь на большой файл: URL_or_path[#md5]")
    p.add_argument("--key", help="ключ идемпотентности (повтор с тем же ключом не создаст дубль)")
    p.add_argument("--claim", help="с -t result: ключ занятого эксперимента, заявка снимается этим сообщением")
    g = p.add_argument_group("поля результата (для журнала)")
    for k in ("metric", "dataset", "value", "err", "baseline", "commit", "weights"):
        g.add_argument("--" + k)


def main():
    p = argparse.ArgumentParser(prog="tg.py", description="telegent: сообщения между агентами команды")
    p.add_argument("--config", help="путь к tg.config.json")
    p.add_argument("--project", help="проект (нужно, только если ты в нескольких)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="отправить")
    add_send_opts(s)
    s = sub.add_parser("dm", help="личные чаты: dm --to имя ТЕКСТ; без адресата — список")
    add_send_opts(s)
    s = sub.add_parser("secret", help="получить адресованный секрет в локальный файл без вывода значения")
    s.add_argument("id", type=int)
    s.add_argument("--out", required=True, help="локальный файл; существующий не перезаписывается")
    s = sub.add_parser("reply", help="ответить на сообщение (топик и адресат наследуются)")
    s.add_argument("id", type=int)
    add_send_opts(s)
    s = sub.add_parser("inbox", help="не подтверждённые (ack) сообщения мне")
    s.add_argument("--since", type=int, help="все сообщения мне после #ID (и прочитанные тоже)")
    s.add_argument("--unread", action="store_true", help="только непрочитанные")
    s.add_argument("--all", action="store_true", help="вся лента, включая мои и чужие адресные")
    s.add_argument("--topic")
    s.add_argument("--type")
    s.add_argument("--author")
    s.add_argument("--open", action="store_true", help="только открытые/взятые просьбы и тревоги")
    s.add_argument("--full", action="store_true", help="печатать сообщения целиком")
    s.add_argument("--limit", type=int, default=50)
    s = sub.add_parser("read", help="прочитать целиком (+ ветка), отмечает прочитанным")
    s.add_argument("id", type=int)
    s.add_argument("--no-mark", action="store_true")
    s.add_argument("--no-thread", action="store_true")
    s = sub.add_parser("react", help="реакция на сообщение: ❤️ 🔥 👍 👎 🤡 😁 😭")
    s.add_argument("id", type=int)
    s.add_argument("emoji", type=lambda emoji: {"+1": "👍", "❤": "❤️"}.get(emoji, emoji),
                   choices=("❤️", "🔥", "👍", "👎", "🤡", "😁", "😭"))
    s.add_argument("--remove", action="store_true", help="убрать свою реакцию")
    s = sub.add_parser("ack", help="подтвердить: обработано, убрать из inbox")
    s.add_argument("ids", type=int, nargs="*")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("watch", help="висеть и печатать строку на каждое новое сообщение (для Monitor)")
    s.add_argument("--since", type=int, help="начать после #ID (по умолчанию — с сохранённого курсора)")
    s.add_argument("--all", action="store_true", help="всю ленту, не только мне")
    s.add_argument("--include-own", action="store_true")
    s.add_argument("--max-burst", type=int, default=30)
    for name, hlp in (("take", "взять просьбу"), ("reopen", "снова открыть")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("id", type=int)
        s.add_argument("--note")
    s = sub.add_parser("done", help="сделано (со ссылкой на результат)")
    s.add_argument("id", type=int)
    s.add_argument("--ref", help="коммит / путь / md5 / #сообщение с результатом")
    s.add_argument("--note")
    s = sub.add_parser("reject", help="отклонить (с причиной)")
    s.add_argument("id", type=int)
    s.add_argument("--note")
    for name in ("approve", "deny"):
        s = sub.add_parser(name, help="(только люди) " + ("одобрить" if name == "approve" else "отказать"))
        s.add_argument("id", type=int)
        s.add_argument("--note")
    s = sub.add_parser("get", help="скачать вложения сообщения")
    s.add_argument("id", type=int)
    s.add_argument("--out", default=".")
    s.add_argument("--name", help="только это вложение")
    s = sub.add_parser("board", help="доска статуса: board | board set --now ... --next ... --gpu-until ...")
    s.add_argument("action", nargs="?", choices=("show", "set"), default="show")
    s.add_argument("--now")
    s.add_argument("--next")
    s.add_argument("--gpu-until", dest="gpu_until")
    s.add_argument("--note")
    s.add_argument("--back", help="когда вернёшься, свободный текст ('~23:00'); пустая строка очищает")
    s = sub.add_parser("claim", help="занять эксперимент (чтобы второй агент не обучал то же самое)")
    s.add_argument("key", help="что занимаешь, например 'resnet18 lr3e-4 seed1' (регистр и пробелы не важны)")
    s.add_argument("-T", "--topic")
    s.add_argument("--note")
    s.add_argument("--until", help="до когда, свободный текст: '03:00'")
    s.add_argument("--hours", type=float, help="срок заявки в часах (по умолчанию 24); повторный claim продлевает")
    s.add_argument("--force", action="store_true", help="перехватить чужую заявку (только по договорённости)")
    s = sub.add_parser("claims", help="кто что занял (по умолчанию активные)")
    s.add_argument("--all", action="store_true", help="и снятые тоже")
    s.add_argument("-T", "--topic")
    s.add_argument("--mine", action="store_true", help="только мои")
    s = sub.add_parser("release", help="снять заявку (ключ или #id)")
    s.add_argument("key_or_id")
    s.add_argument("--note")
    s = sub.add_parser("results", help="журнал результатов")
    s.add_argument("--metric")
    s.add_argument("--dataset")
    s.add_argument("--limit", type=int, default=200)
    s = sub.add_parser("search", help="поиск по истории")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=50)
    sub.add_parser("whoami", help="кто я, мой проект и кто сейчас слушает (у кого жив watch)")
    s = sub.add_parser("projects", help="мои проекты; rename — переименовать свой проект (для человека-создателя)")
    s.add_argument("action", nargs="?", choices=["rename"])
    s.add_argument("title", nargs="?", help="новое название проекта")
    s = sub.add_parser("sent", help="мои сообщения: доставлено / прочитано / ack у каждого адресата")
    s.add_argument("--limit", type=int, default=15)
    s = sub.add_parser("hook", help="для хуков Claude Code: напоминание про watch и непрочитанное")
    s.add_argument("--session-start", action="store_true", help="всегда печатать строку состояния")
    s.add_argument("--status-only", action="store_true",
                   help="(так и так по умолчанию) только статус и номера, без текста сообщений")
    s = sub.add_parser("codex", help="подключение Codex: установка хуков и проверка входящих")
    cs = s.add_subparsers(dest="codex_action", required=True)
    ci = cs.add_parser("install", help="добавить хуки в .codex/hooks.json, сохранив существующие")
    ci.add_argument("--dir", default=".", help="папка проекта Codex (по умолчанию текущая)")
    cs.add_parser("hook", help="обработчик событий Codex: JSON stdin → JSON stdout")
    ci=cs.add_parser('listen',help='мгновенная доставка в выбранную сессию VS Code, включая пробуждение в простое')
    ci.add_argument('--session',help='UUID; по умолчанию текущая сессия из окружения Codex')
    ci.add_argument('--dir',default='.',help='рабочая папка выбранной сессии')
    ci.add_argument('--startup',action='store_true',help='автозапуск при входе в Windows')
    cs.add_parser('listener-status',help='состояние постоянного слушателя')
    cs.add_parser('listener-stop',help='остановить постоянный слушатель')
    s = sub.add_parser("refresh", help="обновить блок инструкции telegent без переустановки хуков")
    s.add_argument("--dir", default=".", help="папка рабочего проекта")
    s.add_argument("--client", choices=("auto", "codex", "claude"), default="auto")
    s = sub.add_parser("update", help="обновить клиент из официального выпуска, сохранив локальные изменения")
    s.add_argument('--automatic', action='store_true', help=argparse.SUPPRESS)
    s = sub.add_parser("init", help="записать tg.config.json")
    s.add_argument("--server", required=True)
    s.add_argument("--token-file", required=True)
    s.add_argument("--secret-file", action="append", help="файл со строками-секретами (не дать им уйти)")
    s = sub.add_parser("join", help="подключиться по ссылке-приглашению: получить токен и записать конфиг")
    s.add_argument("link", help="ссылка от человека (…#invite=tgi_…) или код tgi_…")
    s.add_argument("--token-file", help="куда записать токен (по умолчанию рядом с конфигом: token)")
    s.add_argument("--force", action="store_true", help="перезаписать существующие токен и конфиг")
    s.add_argument("--here", action="store_true",
                   help="конфиг .telegent.json в текущей папке (у каждого агента на машине — свой), токен в ~/.telegent")
    s.add_argument("--url-source", help=argparse.SUPPRESS)
    s.add_argument("--client", choices=("auto", "codex", "claude"), help="сразу настроить интеграцию в текущем проекте")
    s = sub.add_parser("connect", help="подключиться и полностью настроить своего агента одной командой")
    s.add_argument("link", help="ссылка-приглашение или код tgi_…")
    s.add_argument("--client", choices=("auto", "codex", "claude"), default="auto")
    s.add_argument("--token-file", help="куда записать токен (по умолчанию ~/.telegent/<имя>-<метка>.token)")
    s.add_argument("--force", action="store_true", help="заменить существующую настройку по новому приглашению")
    s.add_argument("--url-source", help=argparse.SUPPRESS)
    s.set_defaults(here=True)

    args = p.parse_args()
    PROJECT_ARG[0] = args.project
    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd in ("join", "connect"):
        return cmd_join(args)
    if args.cmd == "codex":
        if args.codex_action == "hook":
            return cmd_codex_hook(args)
        if args.codex_action in ('listen','listener-status','listener-stop'):
            return cmd_codex_receiver(Conf(args.config),args)
        return cmd_codex_install(Conf(args.config), args)
    conf = Conf(args.config)
    if args.cmd != 'update': schedule_update(conf)
    {
        "send": cmd_send, "reply": cmd_reply, "inbox": cmd_inbox, "read": cmd_read, "ack": cmd_ack,
        "watch": cmd_watch, "get": cmd_get, "board": cmd_board, "results": cmd_results,
        "search": cmd_search, "whoami": cmd_whoami, "sent": cmd_sent, "hook": cmd_hook, "projects": cmd_projects,
        "react": cmd_react, "dm": cmd_dm, "secret": cmd_secret, "refresh": cmd_refresh, "update": cmd_update,
        "claim": cmd_claim, "claims": cmd_claims, "release": cmd_release,
        "take": lambda c, a: cmd_status(c, a, "taken"), "done": lambda c, a: cmd_status(c, a, "done"),
        "reject": lambda c, a: cmd_status(c, a, "rejected"), "reopen": lambda c, a: cmd_status(c, a, "open"),
        "approve": lambda c, a: cmd_approve(c, a, "approve"), "deny": lambda c, a: cmd_approve(c, a, "deny"),
    }[args.cmd](conf, args)


if __name__ == "__main__":
    main()
