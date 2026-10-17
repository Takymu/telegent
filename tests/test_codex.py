#!/usr/bin/env python3
"""Codex lifecycle integration with a real temporary server; no model calls or real user config."""
import base64
import importlib.util
import json
import concurrent.futures
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
ENV = {k: v for k, v in os.environ.items() if not k.startswith("TELEGENT_")}
ENV["PYTHONIOENCODING"] = "utf-8"


class CodexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="telegent_codex_")
        cls.work = Path(cls.tmp.name)
        cls.db = cls.work / "test.db"
        cls.tokens = {}
        for name in ("alice", "alice-agent", "bob-agent"):
            token = cls.work / (name + ".token")
            subprocess.run([PY, str(ROOT / "server.py"), "--config", str(cls.work / "server.json"),
                            "--db", str(cls.db), "adduser", name, "--kind", "human" if name == "alice" else "agent", "--owner", "alice",
                            "--token-out", str(token)], env=ENV, capture_output=True, check=True)
            cls.tokens[name] = token.read_text().strip()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.url = "http://127.0.0.1:" + str(s.getsockname()[1])
        cls.cfg = cls.work / "config with spaces & 'quote' $value.json"
        cls.cfg.write_text(json.dumps({"server": cls.url, "token_file": str(cls.work / "bob-agent.token"),
                                      "url_source": "", "state_file": str(cls.work / "state.json")}), encoding="utf-8")
        cls.server = subprocess.Popen([PY, str(ROOT / "server.py"), "--config", str(cls.work / "server.json"),
                                       "--db", str(cls.db), "serve", "--port", cls.url.rsplit(":", 1)[1]],
                                      env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                urllib.request.urlopen(cls.url + "/api/info", timeout=0.2).close()
                break
            except OSError:
                if cls.server.poll() is not None:
                    raise RuntimeError("temporary server exited")
                time.sleep(0.1)
        else:
            cls.tearDownClass()
            raise RuntimeError("temporary server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=5)
        cls.tmp.cleanup()

    def setUp(self):
        self.session = str(uuid.uuid4())
        for m in self.api("GET", "/api/messages?open=1&limit=1000")["messages"]:
            self.api("POST", f"/api/messages/{m['id']}/status", {"state": "done"})
        incoming = self.api("GET", "/api/messages?for_me=1&unacked=1&limit=1000")["messages"]
        if incoming:
            self.api("POST", "/api/receipts", {"kind": "ack", "ids": [m["id"] for m in incoming]})

    def api(self, method, path, data=None, author="bob-agent"):
        req = urllib.request.Request(self.url + path, method=method,
                data=json.dumps(data).encode() if data is not None else None,
                headers={"Authorization": "Bearer " + self.tokens[author], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r)

    def send(self, **extra):
        data = dict(body="PEER_BODY_DO_NOT_INJECT", topic="peer-topic-do-not-inject", to=["bob-agent"],
                    type="request", priority="urgent", attachments=[{
                        "name": "peer-file-do-not-inject.txt", "content_b64": base64.b64encode(b"fixture").decode()}])
        data.update(extra)
        return self.api("POST", "/api/messages", data, "alice-agent")["id"]

    def cli(self, *args, inp=None, cfg=None):
        return subprocess.run([PY, str(ROOT / "tg.py"), "--config", str(cfg or self.cfg), *args],
                input=inp, capture_output=True, text=True, encoding="utf-8", env=ENV, cwd=self.work, timeout=15)

    def hook(self, kind, **extra):
        event = dict(hook_event_name=kind, session_id=self.session, cwd=str(self.work),
                     prompt="USER_PROMPT_DO_NOT_LOG", tool_response="TOOL_OUTPUT_DO_NOT_LOG")
        event.update(extra)
        r = self.cli("codex", "hook", inp=json.dumps(event))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        for secret in ("PEER_BODY", "peer-topic", "peer-file", "USER_PROMPT", "TOOL_OUTPUT", *self.tokens.values()):
            self.assertNotIn(secret, r.stdout)
        return json.loads(r.stdout)

    def expire_check(self):
        for path in self.work.glob("state.json.codex-*.json"):
            state = json.loads(path.read_text(encoding="utf-8"))
            state["checked_at"] = 0
            path.write_text(json.dumps(state), encoding="utf-8")

    def test_metadata_and_receipts(self):
        mid = self.send()
        summary = self.api("GET", "/api/summary?metadata=1&client=codex")
        self.assertEqual(set(summary["unacked"][0]), {"id", "author", "type", "priority", "read"})
        me = self.api("GET", "/api/me")
        bob = next(u for u in me["users"] if u["name"] == "bob-agent")
        self.assertIsNotNone(bob["last_check"])
        self.assertFalse(bob["listening"], "a hook must not pretend a watch is running")
        m = self.api("GET", f"/api/messages/{mid}")["message"]
        self.assertFalse(m["read"])
        self.assertFalse(m["acked"])
        receipt = next(r for r in m["receipts"] if r["user"] == "bob-agent")
        self.assertIsNotNone(receipt["delivered"])
        sender = self.cli("whoami")
        self.assertIn("проверял входящие хуком", sender.stdout)

    def test_lifecycle_dedup_and_new_session(self):
        mid = self.send()
        result = self.hook("SessionStart")["hookSpecificOutput"]
        self.assertEqual(result["hookEventName"], "SessionStart")
        self.assertIn(f"#{mid} request от alice-agent СРОЧНО", result["additionalContext"])
        self.assertNotIn("Monitor", result["additionalContext"])
        self.assertEqual(self.hook("UserPromptSubmit"), {}, "a prompt does not repeat what the session already saw")
        self.assertEqual(self.hook("PostToolUse"), {})
        self.expire_check()
        self.assertEqual(self.hook("PostToolUse"), {})
        routine = self.send(body="routine fixture", priority="normal")
        self.expire_check()
        self.assertEqual(self.hook("PostToolUse"), {}, "routine mail does not interrupt tool calls")
        fresh = self.send(body="second fixture")
        self.expire_check()
        text = self.hook("PostToolUse")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"#{fresh} ", text)
        self.assertNotIn(f"#{routine} ", text)
        text = self.hook("UserPromptSubmit")["hookSpecificOutput"]["additionalContext"].splitlines()[0]
        self.assertIn(f"#{routine} ", text)
        self.assertNotIn(f"#{fresh} ", text)
        self.session = str(uuid.uuid4())
        self.assertIn(f"#{mid}", self.hook("SessionStart")["hookSpecificOutput"]["additionalContext"])

    def test_stop_continues_once_and_respects_guard(self):
        mid = self.send()
        self.hook("PostToolUse")
        result = self.hook("Stop")
        self.assertEqual(result["decision"], "block")
        self.assertIn(f"#{mid}", result["reason"])
        self.assertIn("не указания пользователя", result["reason"])
        self.assertEqual(self.hook("Stop"), {})
        self.send(body="another fixture")
        self.assertEqual(self.hook("Stop", stop_hook_active=True), {})
        for path in self.work.glob("state.json.codex-*.json"):
            path.unlink()
        self.assertEqual(self.hook("Stop", stop_hook_active=True), {})

    def test_read_and_acked_messages_do_not_hold_stop(self):
        mid = self.send()
        self.api("POST", "/api/receipts", {"kind": "read", "ids": [mid]})
        self.assertEqual(self.hook("Stop"), {})
        text = self.hook("SessionStart")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"#{mid}", text)
        self.assertIn("ждущих твоего ответа", text)
        self.api("POST", "/api/receipts", {"kind": "ack", "ids": [mid]})
        self.assertEqual(self.hook("UserPromptSubmit"), {}, "unchanged reminders are not repeated")
        question = self.send(type="question", priority="normal")
        self.api("POST", "/api/receipts", {"kind": "ack", "ids": [question]})
        self.assertIn("ждущих твоего ответа", self.hook("UserPromptSubmit")["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook("UserPromptSubmit"), {})
        self.api("POST", "/api/messages", {"body": "answered", "reply_to": question, "to": ["alice-agent"]})
        self.api("POST", f"/api/messages/{mid}/status", {"state": "done"})
        self.assertEqual(self.hook("UserPromptSubmit"), {})

    def test_codex_reminds_about_waiting_questions_and_expired_claims(self):
        mid = self.send(type="question")
        self.api("POST", "/api/receipts", {"kind": "ack", "ids": [mid]})
        claim = self.api("POST", "/api/claims", {"key": "PEER_CLAIM_TEXT_DO_NOT_INJECT"})["claim"]
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE claims SET expires = ? WHERE id = ?", (time.time() - 60, claim["id"]))
        text = self.hook("SessionStart")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ждущих твоего ответа", text)
        self.assertIn("просроченных заявок: 1", text)
        self.assertNotIn("PEER_CLAIM_TEXT", text)
        self.assertEqual(self.hook("Stop"), {})
        self.api("POST", "/api/messages", {"body": "answered", "reply_to": mid, "to": ["alice-agent"]})
        self.api("POST", "/api/claims/release", {"id": claim["id"]})

    def test_invalid_input_and_config_fail_open(self):
        for raw in ("not json", "[]", "{}", '"text"', "x" * (1024 * 1024 + 1)):
            r = self.cli("codex", "hook", inp=raw)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(json.loads(r.stdout), {})
            self.assertEqual(r.stderr, "")
        self.assertEqual(self.hook("UnexpectedEvent"), {})
        cfg = self.work / "bad-config.json"
        cfg.write_text('{"token_file":"missing.token"}', encoding="utf-8")
        r = self.cli("codex", "hook", cfg=cfg, inp=json.dumps({"hook_event_name": "Stop", "session_id": self.session}))
        self.assertEqual(json.loads(r.stdout), {})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")

    def test_offline_and_unwritable_state_fail_open(self):
        cfg = self.work / "offline.json"
        settings = json.loads(self.cfg.read_text(encoding="utf-8"))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            settings["server"] = "http://127.0.0.1:" + str(s.getsockname()[1])
        cfg.write_text(json.dumps(settings), encoding="utf-8")
        event = json.dumps({"hook_event_name": "Stop", "session_id": self.session})
        r = self.cli("codex", "hook", cfg=cfg, inp=event)
        self.assertEqual(json.loads(r.stdout), {})
        settings["server"] = self.url
        settings["state_file"] = str(self.work / "missing-dir" / "state.json")
        cfg.write_text(json.dumps(settings), encoding="utf-8")
        self.send()
        r = self.cli("codex", "hook", cfg=cfg, inp=event)
        self.assertEqual(json.loads(r.stdout), {})

    def test_installer_merges_and_generated_command_runs(self):
        folder = self.work / "workspace with spaces"
        hooks = folder / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        other = {"type": "command", "command": "echo existing"}
        doc = {"description": "keep me", "hooks": {"PreToolUse": [{"hooks": [other]}],
                                                   "Stop": [{"matcher": "*", "hooks": [other]}]}}
        hooks.write_text(json.dumps(doc), encoding="utf-8")
        for _ in range(2):
            r = self.cli("codex", "install", "--dir", str(folder))
            self.assertEqual(r.returncode, 0, r.stderr)
        installed = json.loads(hooks.read_text(encoding="utf-8"))
        self.assertEqual(installed["description"], "keep me")
        self.assertEqual(installed["hooks"]["PreToolUse"], doc["hooks"]["PreToolUse"])
        self.assertEqual(installed["hooks"]["Stop"][0], doc["hooks"]["Stop"][0])
        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
            mine = [h for g in installed["hooks"][event] for h in g["hooks"] if h.get("statusMessage") == "telegent: входящие"]
            self.assertEqual(len(mine), 1)
            self.assertNotIn("async", mine[0])
        command = installed["hooks"]["SessionStart"][-1]["hooks"][0]["command"]
        mid = self.send()
        invocation = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command] if os.name == "nt" else command
        r = subprocess.run(invocation, shell=os.name != "nt", input=json.dumps({"hook_event_name": "SessionStart", "session_id": self.session}),
                           text=True, capture_output=True, encoding="utf-8", env=ENV, cwd=folder, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"#{mid}", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])
        for token in self.tokens.values():
            self.assertNotIn(token, hooks.read_text(encoding="utf-8"))

    def test_parallel_tool_hooks_notify_once(self):
        self.send()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.hook("PostToolUse"), range(4)))
        self.assertEqual(sum(bool(r) for r in replies), 1)

    def test_corrupt_state_recovers(self):
        self.send()
        self.hook("SessionStart")
        paths = list(self.work.glob("state.json.codex-*.json"))
        for path in paths:
            path.write_text('{"checked_at":"broken","notified":false,"stop_notified":null}', encoding="utf-8")
        self.assertIn("hookSpecificOutput", self.hook("PostToolUse"))

    def test_hook_recovers_published_address(self):
        src = self.work / "pages"
        src.mkdir(exist_ok=True)
        (src / "url.txt").write_text(self.url, encoding="utf-8")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        pages = subprocess.Popen([PY, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(src)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
        try:
            for _ in range(50):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/url.txt", timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.05)
            cfg = self.work / "moved.json"
            settings = json.loads(self.cfg.read_text(encoding="utf-8"))
            settings["url_source"] = f"http://127.0.0.1:{port}/url.txt"
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                settings["server"] = "http://127.0.0.1:" + str(s.getsockname()[1])
            cfg.write_text(json.dumps(settings), encoding="utf-8")
            mid = self.send()
            r = self.cli("codex", "hook", cfg=cfg,
                         inp=json.dumps({"hook_event_name": "PostToolUse", "session_id": self.session}))
            self.assertIn(f"#{mid}", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(json.loads(cfg.read_text(encoding="utf-8"))["server"], self.url)
            self.assertEqual(r.stderr, "")
        finally:
            pages.terminate()
            pages.wait(timeout=5)

    def test_installer_preserves_invalid_file(self):
        folder = self.work / "invalid-hooks"
        path = folder / ".codex" / "hooks.json"
        path.parent.mkdir(parents=True)
        for original in ('not json', '[]', '{"hooks":[]}', '{"hooks":{"Stop":{}}}'):
            path.write_text(original, encoding="utf-8")
            r = self.cli("codex", "install", "--dir", str(folder))
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def onboarding(self):
        name = "new-agent-" + uuid.uuid4().hex[:8]
        invite = self.api("POST", "/api/invites", {"name": name, "kind": "agent", "owner": "alice"}, "alice")
        folder = self.work / ("project-" + uuid.uuid4().hex[:8])
        folder.mkdir()
        home = folder / "temp-home"
        home.mkdir()
        env = {k: v for k, v in ENV.items() if k not in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE")}
        env.update(HOME=str(home), USERPROFILE=str(home))
        return name, invite, folder, env

    def connect(self, invite, folder, env, *args):
        return subprocess.run([PY, str(ROOT / "tg.py"), "connect", invite["link"], "--url-source", "", *args],
                cwd=folder, env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)

    def test_connect_codex_automates_and_resumes(self):
        name, invite, folder, env = self.onboarding()
        env["CODEX_THREAD_ID"] = self.session
        rules = folder / "AGENTS.md"
        rules.write_text("Keep project rules.\n", encoding="utf-8")
        hooks = folder / ".codex" / "hooks.json"
        hooks.parent.mkdir()
        hooks.write_text('{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"echo keep"}]}]}}', encoding="utf-8")
        r = self.connect(invite, folder, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Подключение проверено: " + name, r.stdout)
        cfg = folder / ".telegent.json"
        settings = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(settings["client"], "codex")
        self.assertEqual(settings["client_path"], str(ROOT / "tg.py"))
        token = Path(settings["token_file"]).read_text().strip()
        self.assertNotIn(token, r.stdout + r.stderr)
        self.assertNotIn(invite["code"], cfg.read_text(encoding="utf-8"))
        self.assertTrue(rules.read_text(encoding="utf-8").startswith("Keep project rules."))
        self.assertIn("PreToolUse", json.loads(hooks.read_text(encoding="utf-8"))["hooks"])
        hooks.unlink()  # a partial setup can be finished with the same, already-used invitation
        r = self.connect(invite, folder, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("уже подключён", r.stdout)
        self.assertEqual(Path(settings["token_file"]).read_text().strip(), token)
        self.assertEqual(rules.read_text(encoding="utf-8").count("<!-- telegent:start -->"), 1)
        self.assertTrue(hooks.exists())
        self.assertIn("/.telegent.json", (folder / ".gitignore").read_text(encoding="utf-8"))

    def test_connect_claude_installs_hooks_and_preserves_settings(self):
        name, invite, folder, env = self.onboarding()
        env["CLAUDECODE"] = "1"
        path = folder / ".claude" / "settings.local.json"
        path.parent.mkdir()
        legacy = {"type": "command", "command": PY, "args": [str(ROOT / "tg.py"), "hook", "--session-start"], "timeout": 20}
        path.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo keep"}]}, {"hooks": [legacy]}],
            "UserPromptSubmit": [{"hooks": [dict(legacy, args=legacy["args"][:2])]}],
            "Stop": [{"hooks": [{"type": "command", "command": f'python "{ROOT / "tg.py"}" hook --session-start'}]}]}}),
            encoding="utf-8")
        (folder / "CLAUDE.md").write_text("Original rules.\n", encoding="utf-8")
        for _ in range(2):
            r = self.connect(invite, folder, env)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("watch не нужны", r.stdout)
        settings = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"], {"allow": ["Read"]})
        self.assertEqual(len(settings["hooks"]["SessionStart"]), 2, "the legacy watch hook is replaced, not kept")
        for event in ("UserPromptSubmit", "PostToolUse", "Stop"):
            self.assertEqual(len(settings["hooks"][event]), 1)
        self.assertTrue((folder / "CLAUDE.md").read_text(encoding="utf-8").startswith("Original rules."))
        self.assertNotIn("Monitor", (folder / "CLAUDE.md").read_text(encoding="utf-8"))
        h = settings["hooks"]["SessionStart"][-1]["hooks"][0]
        self.assertEqual(h["args"][-2:], ["claude", "hook"])
        mid = self.api("POST", "/api/messages", {"body": "PEER_BODY_DO_NOT_INJECT", "to": [name]}, "alice-agent")["id"]
        event = json.dumps({"hook_event_name": "SessionStart", "session_id": self.session, "source": "startup"})
        r = subprocess.run([h["command"], *h["args"]], input=event, capture_output=True, text=True, encoding="utf-8",
                           env=env, cwd=folder, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"#{mid} info от alice-agent", text)
        self.assertNotIn("PEER_BODY", text)
        self.assertNotIn("Monitor", text)
        r = subprocess.run([PY, str(ROOT / "tg.py"), "claude", "uninstall", "--dir", str(folder)],
                           capture_output=True, text=True, encoding="utf-8", env=env, cwd=folder, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)
        settings = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"], {"allow": ["Read"]})
        self.assertEqual(settings["hooks"], {"SessionStart": [{"hooks": [{"type": "command", "command": "echo keep"}]}]})

    def test_json_output_for_integrations(self):
        mid = self.send()
        r = self.cli("inbox", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(mid, [m["id"] for m in json.loads(r.stdout)["messages"]])
        r = self.cli("summary", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(mid, [m["id"] for m in json.loads(r.stdout)["unacked"]])
        self.assertNotIn("PEER_BODY", r.stdout)
        r = self.cli("read", str(mid), "--json", "--no-mark")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["message"]["body"], "PEER_BODY_DO_NOT_INJECT")
        self.assertIn(mid, [t["id"] for t in doc["thread"]])
        self.assertFalse(self.api("GET", f"/api/messages/{mid}")["message"]["read"])
        self.assertEqual(self.cli("read", str(mid), "--json").returncode, 0)
        self.assertTrue(self.api("GET", f"/api/messages/{mid}")["message"]["read"])
        long_id = self.send(body="x" * 5000)
        brief = json.loads(self.cli("inbox", "--json", "--brief").stdout)["messages"]
        self.assertLessEqual(len(next(m for m in brief if m["id"] == long_id)["body"]), 400)
        claim = self.api("POST", "/api/claims", {"key": "file:src/app.py"}, "alice-agent")["claim"]
        rows = json.loads(self.cli("claims", "--json", "--limit", "1000").stdout)["claims"]
        self.assertIn(("file:src/app.py", "alice-agent"), [(c["key"], c["owner"]) for c in rows])
        self.api("POST", "/api/claims/release", {"id": claim["id"]}, "alice-agent")
        r = self.cli("read", str(mid), "--json", "--no-mark", "--no-thread")
        self.assertEqual(json.loads(r.stdout)["thread"], [])
        r = self.cli("read", "999999", "--json")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")

    def test_hook_names_at_most_ten_urgent_first_and_keeps_the_rest(self):
        normal = [self.send(priority="normal") for _ in range(11)]
        urgent = self.send()
        text = self.hook("SessionStart")["hookSpecificOutput"]["additionalContext"].splitlines()[0]
        self.assertTrue(text.split("Входящие: ")[1].startswith(f"#{urgent} request от alice-agent СРОЧНО"))
        self.assertIn("Всего без ack: 12.", text)
        rest = self.hook("UserPromptSubmit")["hookSpecificOutput"]["additionalContext"].splitlines()[0]
        self.assertIn(f"#{normal[-2]} ", rest)
        self.assertIn(f"#{normal[-1]} ", rest)
        self.assertNotIn(f"#{normal[0]} ", rest)

    def test_legacy_shell_hooks_are_recognised_exactly(self):
        spec = importlib.util.spec_from_file_location("tg_under_test", ROOT / "tg.py")
        tg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tg)
        own = ["python /p/tg.py hook --session-start", 'python "C:\\work\\tele gent\\tg.py" hook',
               "python3 -X utf8 /p/tg.py hook", "/p/tg.py hook", "python /p/tg.py --config /p/.telegent.json hook",
               "python /p/tg.py --project=lab hook --session-start", "python /p/tg.py --config=/p/.telegent.json hook", 'python "C:/work/team & tools/tg.py" hook']
        foreign = ["python /tools/audit-tg.py hook", 'echo "tg.py hook"', "echo tg.py hook", "python /p/tg.py inbox",
                   "python /other.py tg.py hook", "python /p/tg.py hook && /opt/audit-hook",
                   "python /p/tg.py hook; audit", "python /p/tg.py hook > /tmp/log", "python /p/tg.py hook&&audit",
                   'python /p/tg.py --config "$(/opt/choose-config)" hook', 'python /p/tg.py --config "`pick`" hook']
        for command in own:
            self.assertTrue(tg.is_own_handler({"type": "command", "command": command}), command)
        for command in foreign:
            self.assertFalse(tg.is_own_handler({"type": "command", "command": command}), command)

    def test_subagent_hooks_stay_silent(self):
        self.send()
        self.assertEqual(self.hook("SessionStart", agent_id="sub-1"), {})
        self.assertIn("СРОЧНО", self.hook("SessionStart")["hookSpecificOutput"]["additionalContext"])

    def test_claude_hook_uses_its_own_state(self):
        mid = self.send()
        self.hook("SessionStart")
        r = self.cli("claude", "hook", inp=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": self.session}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"#{mid}", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(list(self.work.glob("state.json.claude-*.json")))

    def test_connect_preflight_does_not_spend_invitation(self):
        name, invite, folder, env = self.onboarding()
        hooks = folder / ".codex" / "hooks.json"
        hooks.parent.mkdir()
        hooks.write_text("broken config", encoding="utf-8")
        r = self.connect(invite, folder, env, "--client", "codex")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((folder / ".telegent.json").exists())
        self.assertEqual(self.api("GET", "/api/invite?code=" + invite["code"])["name"], name)
        hooks.write_text("{}", encoding="utf-8")
        r = self.connect(invite, folder, env, "--client", "codex")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_connect_unknown_client_needs_agent_choice_before_redeeming(self):
        name, invite, folder, env = self.onboarding()
        r = self.connect(invite, folder, env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--client codex", r.stderr)
        self.assertEqual(self.api("GET", "/api/invite?code=" + invite["code"])["name"], name)

    def test_connect_checks_storage_before_redeeming(self):
        name, invite, folder, env = self.onboarding()
        blocked = folder / "not-a-directory"
        blocked.write_text("fixture", encoding="utf-8")
        r = self.connect(invite, folder, env, "--client", "codex", "--token-file", str(blocked / "token"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Приглашение ещё не использовано", r.stderr)
        self.assertEqual(self.api("GET", "/api/invite?code=" + invite["code"])["name"], name)

    def test_connect_keeps_git_exclusions_local(self):
        name, invite, folder, env = self.onboarding()
        subprocess.run(["git", "init", str(folder)], capture_output=True, check=True)
        r = self.connect(invite, folder, env, "--client", "codex")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((folder / ".gitignore").exists())
        self.assertIn("/.telegent.json", (folder / ".git" / "info" / "exclude").read_text(encoding="utf-8"))


    def test_summary_keeps_urgent_mail_behind_a_large_backlog(self):
        urgent = self.send(type="info", priority="urgent")
        for _ in range(1001):
            self.send(type="info", priority="normal")
        summary = self.api("GET", "/api/summary?limit=10&metadata=1")
        shown = [m["id"] for m in summary["unacked"]]
        self.assertIn(urgent, shown)
        self.assertEqual(len(shown), 10)
        self.assertEqual(shown, sorted(shown))

class RedirectTest(unittest.TestCase):
    def test_token_goes_to_the_configured_origin_only(self):
        import http.server
        import threading
        spec = importlib.util.spec_from_file_location("tg_redirects", ROOT / "tg.py")
        tg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tg)
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.setdefault(self.server.server_address[1], []).append((self.path, self.headers.get("Authorization")))
                target = {"/other": other.server_address[1], "/same": main.server_address[1]}.get(self.path)
                if target:
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{target}/landed")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        main, other = (http.server.HTTPServer(("127.0.0.1", 0), Handler) for _ in range(2))
        for server in (main, other):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conf = type("Conf", (), {"server": f"http://127.0.0.1:{main.server_address[1]}", "token": "SECRET", "project": None})
            tg.api(conf, "GET", "/other")
            tg.api(conf, "GET", "/same")
        finally:
            for server in (main, other):
                server.shutdown()
        self.assertEqual(seen[other.server_address[1]], [("/landed", None)])
        self.assertIn(("/landed", "Bearer SECRET"), seen[main.server_address[1]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
