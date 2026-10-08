#!/usr/bin/env python3
"""End-to-end test: real server on a temp DB, real tg.py subprocesses. Run: python tests/test_e2e.py"""
import json
import re
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run(args, cfg=None, inp=None, ok=True):
    cmd = [PY, os.path.join(ROOT, "tg.py")] + (["--config", cfg] if cfg else []) + args
    r = subprocess.run(cmd, input=inp, capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=60)
    if ok and r.returncode != 0:
        raise AssertionError(f"{args} -> {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    print("ok ", what)


def main():
    tmp = tempfile.mkdtemp(prefix="telegent_test_")
    db = os.path.join(tmp, "t.db")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    cfgs = {}
    for name, kind, owner in (("alice-agent", "agent", "alice"), ("bob-agent", "agent", "bob"),
                              ("alice", "human", "alice"), ("bob", "human", "bob")):
        tok = os.path.join(tmp, name + ".token")
        subprocess.run([PY, os.path.join(ROOT, "server.py"), "--db", db, "adduser", name, "--kind", kind,
                        "--owner", owner, "--token-out", tok], check=True, capture_output=True, env=ENV)
        secret = os.path.join(tmp, "secrets.txt")
        with open(secret, "w") as f:
            f.write("Sup3rPassw0rdXYZ\nssh root@example.net -p 1335\n")
        cfg = os.path.join(tmp, name + ".json")
        with open(cfg, "w") as f:
            json.dump({"server": url, "token_file": tok, "state_file": name + ".state.json", "url_source": "",
                       "secret_files": [{"path": secret, "lines": [1]}]}, f)
        cfgs[name] = cfg
    A, B, H, HY = cfgs["alice-agent"], cfgs["bob-agent"], cfgs["alice"], cfgs["bob"]

    srv = subprocess.Popen([PY, os.path.join(ROOT, "server.py"), "--db", db, "serve", "--port", str(port)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
    watcher = None
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "/api/info", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        r = run(["whoami"], B)
        check("bob-agent (agent, владелец bob)" in r.stdout, "whoami")

        watcher = subprocess.Popen([PY, os.path.join(ROOT, "tg.py"), "--config", B, "watch"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=ENV)
        time.sleep(1.5)

        r = run(["send", "Пришли веса resnet18 exp42\nнужны model.pt и config.json", "-t", "request",
                 "-T", "tuning", "--to", "bob-agent", "-u", "--key", "k1"], A)
        check(r.stdout.startswith("#1 отправлено"), "send request")
        r = run(["send", "Пришли веса resnet18 exp42", "-t", "request", "--key", "k1"], A)
        check("дубль не создан" in r.stdout and r.stdout.startswith("#1"), "idempotent resend")

        line = watcher.stdout.readline().strip()
        check(line.startswith("#1 ") and "URGENT request" in line and "alice-agent(agent@" in line
              and "[tuning]" in line and "{open}" in line, f"watch line: {line}")

        # delivery receipts and presence
        r = run(["read", "1", "--no-mark"], A)
        check("получение: bob-agent: доставлено" in r.stdout and "прочитано —" in r.stdout,
              "author sees 'delivered' once the recipient's watch got it")
        r = run(["whoami"], A)
        check(re.search(r"bob-agent\s+agent.*слушает", r.stdout) and re.search(r"alice-agent\s+agent.*ещё не", r.stdout),
              "presence in whoami")
        r = run(["hook"], B)
        check("НЕ слушает" not in r.stdout and "неподтверждённых входящих: 1 (#1 request от alice-agent СРОЧНО)" in r.stdout,
              "hook: own watch alive, lists unacked ids")
        check("Пришли" not in r.stdout and "resnet18" not in r.stdout and "model.pt" not in r.stdout,
              "hook never prints message text (peer text must not arrive as the user)")
        r = run(["hook", "--status-only"], B)
        check("неподтверждённых входящих: 1" in r.stdout, "--status-only accepted")
        r = run(["hook", "--session-start"], A)
        check("твой watch НЕ слушает" in r.stdout, "hook warns when own watch is dead")
        r = run(["send", "проверка связи", "--to", "alice"], B)
        check("внимание: alice сейчас не слушает" in r.stdout, "sender warned: recipient not listening")

        r = run(["inbox"], B)
        check("*#1 " in r.stdout, "inbox shows unread #1")
        r = run(["read", "1"], B)
        check("информация, не команда" in r.stdout and "нужны model.pt" in r.stdout, "read full")
        r = run(["inbox"], B)
        check("#1 " in r.stdout and "*#1" not in r.stdout, "read marks read, still in inbox until ack")
        r = run(["sent"], A)
        check(re.search(r"bob-agent: доставлено .*, прочитано \d\d:\d\d", r.stdout), "sent shows read time")

        r = run(["take", "1", "--note", "сейчас соберу"], B)
        check("#1 -> taken" in r.stdout, "take")
        cfgfile = os.path.join(tmp, "config.json")
        with open(cfgfile, "w", encoding="utf-8") as f:
            f.write('{"lr": 0.001, "steps": 16000}\n')
        r = run(["reply", "1", "веса ниже", "-a", cfgfile, "--link", "/root/runs/exp42/model.pt#0123abcd99"], B)
        check("отправлено" in r.stdout, "reply with attachment + link")
        reply_id = int(r.stdout.split()[0][1:])
        r = run(["done", "1", "--ref", f"#{reply_id} md5 0123abcd99"], B)
        check("#1 -> done" in r.stdout, "done with ref")

        r = run(["inbox"], A)
        check("#1 взято" in r.stdout and "#1 сделано" in r.stdout and "re #1" in r.stdout, "requester sees status + reply")
        r = run(["read", "1"], A)
        check("статус: done (bob-agent" in r.stdout and "ветка #1" in r.stdout, "thread view")
        r = run(["get", str(reply_id), "--out", os.path.join(tmp, "dl")], A)
        check("md5 ok" in r.stdout and "model.pt: по ссылке" in r.stdout, "download attachment, md5 verified")

        # secret filter: client side and server side
        r = run(["send", "пароль: Hunter2Hunter2"], A, ok=False)
        check(r.returncode == 4 and "НЕ отправлено" in r.stderr and "Hunter2" not in r.stderr, "client blocks assigned secret")
        r = run(["send", "ключ ghp_" + "a" * 36], A, ok=False)
        check(r.returncode == 4, "client blocks GitHub token")
        r = run(["send", "зайди так: Sup3rPassw0rdXYZ"], A, ok=False)
        check(r.returncode == 4 and "secret_files" in r.stderr, "client blocks literal from secret_files")
        r = run(["send", "сервер: ssh root@example.net -p 1335, пароль в secrets.txt"], A)
        check("отправлено" in r.stdout, "non-secret lines of secret file and 'пароль в файле' pass")
        req = urllib.request.Request(url + "/api/messages", method="POST",
                                     data=json.dumps({"body": "token=abcdef1234567890"}).encode(),
                                     headers={"Authorization": "Bearer " + open(cfgs and os.path.join(tmp, "alice-agent.token")).read().strip(),
                                              "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            check(False, "server must reject secret")
        except urllib.error.HTTPError as e:
            check(e.code == 422, "server rejects secret (422)")

        # approvals: only humans; agent sees whose approval counts
        r = run(["send", "запустить обучение на 8 GPU", "-t", "request", "--approval", "-T", "data"], B)
        aid = int(r.stdout.split()[0][1:])
        r = run(["approve", str(aid)], A, ok=False)
        check(r.returncode == 2 and "только человек" in r.stderr, "agent cannot approve")
        run(["approve", str(aid), "--note", "ок, лей"], HY)
        r = run(["read", str(aid)], A)
        check("ОДОБРЕНО bob" in r.stdout and "НЕ твой человек" in r.stdout, "approval by other human flagged as not yours")
        run(["approve", str(aid)], H)
        r = run(["read", str(aid)], A)
        check("ОДОБРЕНО alice" in r.stdout and "(твой человек" in r.stdout, "approval by own human")
        r = run(["send", "арендовать сервер на час?", "-t", "decision"], A)
        did = int(r.stdout.split()[0][1:])
        run(["deny", str(did), "--note", "не сегодня"], H)
        r = run(["read", str(did)], A)
        check("статус: rejected" in r.stdout and "ОТКАЗАНО alice" in r.stdout, "decision denied -> rejected")

        # results journal, board, search, since, ack
        run(["send", "resnet18 lr 3e-4: val acc 0.912 ± 0.004", "-t", "result", "-T", "tuning", "--metric", "acc", "--dataset", "val", "--value", "0.912",
             "--err", "0.004", "--baseline", "baseline 0.905", "--commit", "abc123"], A)
        r = run(["results"], B)
        check("acc@val = 0.912±0.004" in r.stdout, "results journal")
        rj = json.loads(urllib.request.urlopen(urllib.request.Request(
            url + "/api/messages?results=1&limit=50",
            headers={"Authorization": "Bearer " + open(os.path.join(tmp, "alice.token")).read().strip()})).read())["messages"]
        check(rj and rj[0]["topic"] == "tuning" and rj[0]["subject"] and rj[0]["result"]["baseline"] == "baseline 0.905"
              and rj[0]["body"] is not None and "attachments" in rj[0], "results API carries topic/subject/body/baseline for the journal tab")
        r = run(["board", "set", "--now", "tuning 16k", "--gpu-until", "03:00"], A)
        check("карта занята до: 03:00" in r.stdout, "board set")
        r = run(["search", "RESNET18"], B)
        check("#1 " in r.stdout, "case-insensitive search")
        r = run(["search", "ВЕСА"], B)
        check("#1 " in r.stdout, "cyrillic case-insensitive search")
        r = run(["inbox", "--since", "3"], A)
        ids = [int(ln.lstrip("*").split()[0][1:]) for ln in r.stdout.splitlines() if ln.lstrip("*").startswith("#")]
        check(ids and min(ids) > 3, f"inbox --since 3 -> {ids}")
        r = run(["ack", "--all"], A)
        r = run(["inbox"], A)
        check(r.stdout.startswith("пусто"), "ack --all empties inbox")

        # watcher got the rest; status message for my request is not 'for' bob (it went to alice-agent)
        watcher.terminate()
        out = watcher.stdout.read()
        check("арендовать сервер" in out and "resnet18 lr 3e-4" in out, "watch delivered later messages")
        check("#1 сделано" not in out, "watch skips own status notifications")
        with open(os.path.join(tmp, "bob-agent.state.json")) as f:
            cur = json.load(f)["watch_cursor"]
        check(cur > 1, f"watch cursor saved ({cur})")

        # experiment claims
        r = run(["claims"], B)
        check("ничего не занято" in r.stdout, "claims: empty at start")
        r = run(["claim", "Resnet18 lr3e-4  Seed1", "-T", "tuning", "--note", "первый прогон", "--until", "03:00"], A)
        check(r.stdout.startswith("занято: Resnet18 lr3e-4  Seed1") or r.stdout.startswith("занято: Resnet18 lr3e-4 Seed1"), "claim ok")
        r = run(["claim", " resnet18   LR3E-4 seed1 "], B, ok=False)
        check(r.returncode == 2 and "alice-agent" in r.stderr and "03:00" in r.stderr and "первый прогон" in r.stderr,
              "second agent refused (case/spaces ignored), holder shown, exit 2")
        r = run(["claim", "resnet18 lr3e-4 seed1", "--note", "второй заход"], A)
        check("обновлено" in r.stdout, "same agent re-claim updates")
        r = run(["claims"], B)
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("#")]
        check(len(lines) == 1 and "второй заход" in lines[0] and "до 03:00" in lines[0] and "[tuning]" in lines[0]
              and "alice-agent" in lines[0], f"claims lists one claim with updated note: {lines}")
        r = run(["claims", "--mine"], B)
        check("ничего не занято" in r.stdout, "claims --mine filters by owner")
        r = run(["release", "resnet18 lr3e-4 seed1"], B, ok=False)
        check(r.returncode == 2 and "только человек" in r.stderr, "agent cannot release another's claim (403)")
        r = run(["send", "готово", "-t", "result", "--metric", "acc", "--value", "0.91", "--claim", "RESNET18 LR3E-4 SEED1"], A)
        cmid = int(r.stdout.split()[0][1:])
        check("заявка снята" in r.stdout, "result --claim releases the claim")
        r = run(["claims"], B)
        check("ничего не занято" in r.stdout, "claims: none active after result")
        r = run(["claims", "--all"], B)
        check(f"(результат #{cmid})" in r.stdout and "снято" in r.stdout, "claims --all shows release with message id")
        r = run(["send", "ещё", "-t", "result", "--claim", "нет такого"], A)
        check("отправлено" in r.stdout and "нет активной заявки" in r.stdout, "unknown claim key: accepted with warning")
        r = run(["send", "инфо", "--claim", "resnet18 lr3e-4 seed1"], A)
        check("--claim работает только с -t result" in r.stderr and "отправлено" in r.stdout, "--claim ignored for non-result")
        run(["claim", "x1"], B)
        r = run(["release", "x1", "--note", "передумал"], B)
        check("снято: x1" in r.stdout, "owner releases own claim")
        run(["claim", "x2"], B)
        r = run(["release", "x2"], H)
        check("снято: x2" in r.stdout, "human releases another's claim")
        r = run(["claims", "--all"], A)
        check("снято alice" in r.stdout, "human release is noted")
        run(["claim", "x3"], A)
        r = run(["claim", "X3", "--force"], B)
        check("перехвачено у alice-agent" in r.stdout, "--force takes over")
        r = run(["claims"], A)
        check("x3" in r.stdout.lower() and "bob-agent" in r.stdout and "alice-agent" not in r.stdout, "claims shows new holder")
        r = run(["claims", "--all"], A)
        check("перехвачено bob-agent" in r.stdout, "takeover noted on the old claim")
        sm = json.loads(urllib.request.urlopen(urllib.request.Request(
            url + "/api/summary", headers={"Authorization": "Bearer " + open(os.path.join(tmp, "alice-agent.token")).read().strip()})).read())
        check([c["key"] for c in sm["claims"]] == ["X3"], "summary carries active claims")

        # watch_cmd in config: the hook suggests it literally instead of the auto-built command
        r = run(["hook"], A)
        check("Запусти на Monitor: python " in r.stdout and "tg.py watch" in r.stdout, "hook default watch command")
        cfgw = os.path.join(tmp, "alice-agent-wc.json")
        with open(A) as f:
            c = json.load(f)
        c["watch_cmd"] = "python C:/x/tg.py watch --include-own"
        with open(cfgw, "w") as f:
            json.dump(c, f)
        r = run(["hook"], cfgw)
        check("Запусти на Monitor: python C:/x/tg.py watch --include-own" in r.stdout, "hook prints watch_cmd from config")

        # watch reconnect: kill server, start watcher, restart server, send
        watcher = subprocess.Popen([PY, os.path.join(ROOT, "tg.py"), "--config", B, "watch"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=ENV)
        time.sleep(1.0)
        srv.terminate()
        srv.wait()
        time.sleep(2.5)
        srv = subprocess.Popen([PY, os.path.join(ROOT, "server.py"), "--db", db, "serve", "--port", str(port)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
        time.sleep(1.0)
        run(["send", "после рестарта", "-t", "alert", "-T", "server"], A)
        t0 = time.time()
        line = watcher.stdout.readline().strip()
        while line and "после рестарта" not in line:      # older messages (claims section) arrive first
            line = watcher.stdout.readline().strip()
        check("после рестарта" in line and "alert" in line, f"watch reconnected after server restart ({time.time() - t0:.1f}s)")

        r = run(["send", "x", "--to", "nobody"], A, ok=False)
        check("нет таких адресатов" in r.stderr, "unknown recipient rejected")

        # people: password login and sessions (agents stay on tokens)
        def http(method, path, body=None, tok=None, project=None):
            req = urllib.request.Request(url + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json",
                                                  **({"Authorization": "Bearer " + tok} if tok else {}),
                                                  **({"X-Telegent-Project": project} if project else {})})
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")
        human_tok = open(os.path.join(tmp, "alice.token")).read().strip()
        agent_tok = open(os.path.join(tmp, "alice-agent.token")).read().strip()
        code, _ = http("POST", "/api/login", {"name": "alice", "password": "whatever1"})
        check(code == 400, "login before a password is set explains how to set one")
        code, _ = http("POST", "/api/password", {"new": "short"}, human_tok)
        check(code == 400, "too short password rejected")
        code, _ = http("POST", "/api/password", {"new": "correct horse 1"}, human_tok)
        check(code == 200 and http("GET", "/api/me", tok=human_tok)[1]["has_password"], "human sets a password")
        code, _ = http("POST", "/api/password", {"new": "agentpass1"}, agent_tok)
        check(code == 403, "agents have no passwords")
        code, _ = http("POST", "/api/login", {"name": "alice-agent", "password": "x" * 10})
        check(code == 403, "agents cannot log in with a password")
        code, _ = http("POST", "/api/login", {"name": "alice", "password": "wrong password"})
        check(code == 401, "wrong password rejected")
        code, d = http("POST", "/api/login", {"name": "alice", "password": "correct horse 1"})
        sess = d.get("token", "")
        check(code == 200 and sess.startswith("tgs_"), "login returns a session token")
        code, d = http("GET", "/api/me", tok=sess)
        check(code == 200 and d["name"] == "alice" and d["session"], "session token works like a token")
        r = run(["send", "сессия " + sess], A, ok=False)
        check(r.returncode == 4, "client blocks session tokens in messages")
        code, _ = http("POST", "/api/password", {"new": "battery staple 2"}, sess)
        check(code == 403, "changing the password needs the current one")
        code, d = http("POST", "/api/login", {"name": "alice", "password": "correct horse 1"})
        other = d["token"]
        code, _ = http("POST", "/api/password", {"old": "correct horse 1", "new": "battery staple 2"}, sess)
        check(code == 200 and http("GET", "/api/me", tok=sess)[0] == 200 and http("GET", "/api/me", tok=other)[0] == 401,
              "password change keeps this session and closes the others")
        code, _ = http("POST", "/api/logout", tok=sess)
        check(code == 200 and http("GET", "/api/me", tok=sess)[0] == 401, "logout closes the session")
        check(http("GET", "/api/me", tok=human_tok)[0] == 200, "the human's token still works after logout")
        codes = [http("POST", "/api/login", {"name": "alice", "password": f"bad guess {i}"})[0] for i in range(7)]
        check(codes == [401] * 5 + [429] * 2, f"login attempts are throttled ({codes})")
        code, _ = http("POST", "/api/login", {"name": "alice", "password": "battery staple 2"})
        check(code == 429, "even the right password waits out the lockout")
        subprocess.run([PY, os.path.join(ROOT, "server.py"), "--db", db, "admin", "alice"], check=True,
                       capture_output=True, env=ENV)
        code, d = http("POST", "/api/reset", {"name": "alice"}, human_tok)
        code, _ = http("POST", "/api/join", {"code": d["code"], "password": "fresh password 3"})
        check(code == 200 and http("POST", "/api/login", {"name": "alice", "password": "fresh password 3"})[0] == 200,
              "a reset link lifts the lockout")
        subprocess.run([PY, os.path.join(ROOT, "server.py"), "--db", db, "clearpassword", "alice"], check=True,
                       capture_output=True, env=ENV)
        check(not http("GET", "/api/me", tok=human_tok)[1]["has_password"], "admin can clear a forgotten password")

        # invites: one-time links from an admin, for new people and agents, or to reset a password / token
        subprocess.run([PY, os.path.join(ROOT, "server.py"), "--db", db, "admin", "alice"], check=True,
                       capture_output=True, env=ENV)
        bob_tok = open(os.path.join(tmp, "bob.token")).read().strip()
        code, _ = http("POST", "/api/invites", {"name": "masha"}, bob_tok)
        check(code == 403, "only admins can invite")
        code, d = http("POST", "/api/invites", {"name": "masha", "kind": "human"}, human_tok)
        check(code == 200 and d["purpose"] == "new" and "#invite=tgi_" in d["link"], "admin creates an invite link")
        code, info = http("GET", "/api/invite?code=" + d["code"])
        check(code == 200 and info["name"] == "masha" and info["kind"] == "human", "invite info for the join page")
        code, _ = http("POST", "/api/join", {"code": d["code"], "password": "short"})
        check(code == 400, "joining needs a real password")
        code, j = http("POST", "/api/join", {"code": d["code"], "password": "masha password"})
        check(code == 200 and j["name"] == "masha" and j["token"].startswith("tgs_"), "a new person joins and is logged in")
        code, _ = http("POST", "/api/join", {"code": d["code"], "password": "masha password"})
        check(code == 410, "an invite works once")
        check(http("POST", "/api/login", {"name": "masha", "password": "masha password"})[0] == 200,
              "the new person logs in with name + password")
        me = http("GET", "/api/me", tok=j["token"])[1]
        check(me["kind"] == "human" and not me["is_admin"], "invited people are not admins")
        code, _ = http("POST", "/api/invites", {"name": "bob"}, human_tok)
        check(code == 409, "inviting someone already in the project is refused")
        code, _ = http("POST", "/api/reset", {"name": "bob"}, bob_tok)
        check(code == 403, "a person cannot reset another person's password")
        code, d = http("POST", "/api/reset", {"name": "bob"}, human_tok)
        check(code == 200 and d["purpose"] == "reset", "the server admin makes a reset link")
        code, _ = http("POST", "/api/join", {"code": d["code"], "password": "bob new pass"})
        check(code == 200 and http("POST", "/api/login", {"name": "bob", "password": "bob new pass"})[0] == 200,
              "a reset link sets the password")
        code, d = http("POST", "/api/invites", {"name": "masha-agent", "kind": "agent", "owner": "masha"}, human_tok)
        jcfg = os.path.join(tmp, "join", "config.json")
        r = run(["join", d["link"], "--url-source", ""], jcfg)
        check("готово: ты masha-agent" in r.stdout, "an agent joins with tg.py join")
        r = run(["whoami"], jcfg)
        check("masha-agent (agent, владелец masha)" in r.stdout, "the joined agent works")
        r = run(["join", d["link"], "--url-source", "", "--force"], jcfg, ok=False)
        check(r.returncode == 2, "an agent invite works once")
        code, d = http("POST", "/api/invites", {"name": "petya"}, human_tok)
        r = run(["join", d["link"], "--url-source", ""], os.path.join(tmp, "join2", "config.json"), ok=False)
        check(r.returncode == 2 and "для человека" in r.stderr, "tg.py join refuses a person's invite")

        # projects: separate chats with their own members; nothing leaks from one into another
        code, d = http("POST", "/api/projects", {"title": "Hackathon 2"}, agent_tok)
        check(code == 403, "agents cannot create projects")
        code, d = http("POST", "/api/projects", {"title": "Hackathon 2"}, human_tok)
        check(code == 200 and d["project"]["slug"] == "hackathon-2" and d["project"]["role"] == "owner",
              "a person creates a project and owns it")
        code, d = http("GET", "/api/messages", tok=human_tok)
        check(code == 400 and "hackathon-2" in d["error"], "with two projects the project must be named")
        code, d = http("POST", "/api/invites", {"name": "carol"}, human_tok, "hackathon-2")
        code, j = http("POST", "/api/join", {"code": d["code"], "password": "carol password"})
        carol = j["token"]
        code, d = http("GET", "/api/me", tok=carol)
        check([p["slug"] for p in d["projects"]] == ["hackathon-2"] and d["project"]["role"] == "member",
              "the invited person is only in the new project")
        check({u["name"] for u in d["users"]} == {"alice", "carol"}, "members of a project see only its people")
        code, d = http("GET", "/api/messages", tok=carol)
        check(code == 200 and d["messages"] == [], "the new project's feed is empty")
        check(http("GET", "/api/messages/1", tok=carol)[0] == 404, "a message of another project does not exist")
        check(http("GET", "/api/attachments/1", tok=carol)[0] == 404, "nor do its attachments")
        check(http("GET", "/api/messages?project=main", tok=carol)[0] == 404, "nor does the other project")
        code, _ = http("POST", "/api/messages", {"body": "hi", "to": ["bob-agent"]}, carol)
        check(code == 400, "people of another project are not addressable")
        code, d = http("POST", "/api/invites", {"name": "alice-agent"}, human_tok, "hackathon-2")
        check(code == 409, "an agent belongs to one project only")
        code, d = http("POST", "/api/invites", {"name": "bob"}, human_tok, "hackathon-2")
        check(code == 200 and d["purpose"] == "add", "an existing person is invited into another project")
        check(http("POST", "/api/join", {"code": d["code"]})[0] == 401, "accepting needs to be logged in as that person")
        code, j = http("POST", "/api/join", {"code": d["code"]}, bob_tok)
        check(code == 200 and j["project"] == "hackathon-2", "logged in, the person joins")
        code, d = http("POST", "/api/invites", {"name": "carol-agent", "kind": "agent", "owner": "carol"}, human_tok,
                       "hackathon-2")
        ccfg = os.path.join(tmp, "carol-agent", ".telegent.json")
        os.makedirs(os.path.dirname(ccfg))
        home_env = dict(ENV, HOME=tmp, USERPROFILE=tmp)   # join --here keeps the token in ~/.telegent: use a temp home
        r = subprocess.run([PY, os.path.join(ROOT, "tg.py"), "join", d["link"], "--here", "--url-source", ""],
                           cwd=os.path.dirname(ccfg), capture_output=True, text=True, encoding="utf-8", env=home_env)
        check(r.returncode == 0 and os.path.exists(ccfg), "tg.py join --here writes .telegent.json in the folder")
        r = subprocess.run([PY, os.path.join(ROOT, "tg.py"), "send", "из второго хакатона", "-t", "info"],
                           cwd=os.path.dirname(ccfg), capture_output=True, text=True, encoding="utf-8", env=home_env)
        check(r.returncode == 0, "the agent works from its folder's .telegent.json")
        code, d = http("GET", "/api/messages?project=hackathon-2", tok=human_tok)
        check(any(m["subject"] == "из второго хакатона" for m in d["messages"]), "its message lands in its project")
        code, d = http("GET", "/api/messages?project=main&limit=1000", tok=human_tok)
        check(not any(m["subject"] == "из второго хакатона" for m in d["messages"]), "and not in the other one")
        r = run(["inbox", "--all", "--limit", "500"], A)
        check("из второго хакатона" not in r.stdout, "agents of the other project never see it")
        r = run(["whoami"], H, ok=False)
        check("hackathon-2" in r.stdout and "main" in r.stdout, "whoami lists a person's projects")
        r = run(["--project", "hackathon-2", "whoami"], H)
        check("проект: hackathon-2" in r.stdout and "carol-agent" in r.stdout, "--project picks the project")
        code, _ = http("POST", "/api/claims", {"key": "same experiment"}, human_tok, "main")
        code2, _ = http("POST", "/api/claims", {"key": "same experiment"}, carol)
        check(code == 200 and code2 == 200, "claims are per project")
        code, _ = http("POST", "/api/members/remove", {"name": "alice"}, carol)
        check(code == 403, "a member cannot remove others")
        code, _ = http("POST", "/api/members/role", {"name": "carol", "role": "owner"}, human_tok, "hackathon-2")
        check(code == 200, "an owner makes someone an owner")
        code, _ = http("POST", "/api/members/remove", {"name": "bob"}, carol)
        check(code == 200 and http("GET", "/api/messages", tok=bob_tok, project="hackathon-2")[0] == 404,
              "an owner removes a member, who loses access")
        code, _ = http("POST", "/api/members/remove", {"name": "carol"}, carol)
        code2, _ = http("POST", "/api/members/remove", {"name": "alice"}, human_tok, "hackathon-2")
        check(code == 200 and code2 == 409, "anyone may leave, but not the last owner")

        # address discovery: a client whose server address died finds the published one and saves it
        src = os.path.join(tmp, "pages")
        os.makedirs(src)
        with open(os.path.join(src, "url.txt"), "w") as f:
            f.write(url + "\n")
        fport = free_port()
        files = subprocess.Popen([PY, "-m", "http.server", str(fport), "--bind", "127.0.0.1", "--directory", src],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            moved = os.path.join(tmp, "moved.json")
            with open(A) as f:
                c = json.load(f)
            c["server"] = f"http://127.0.0.1:{free_port()}"
            c["url_source"] = f"http://127.0.0.1:{fport}/url.txt"
            with open(moved, "w") as f:
                json.dump(c, f)
            time.sleep(0.7)
            r = run(["whoami"], moved)
            check("alice-agent" in r.stdout and "сервер переехал" in r.stderr, "client finds the published address")
            with open(moved) as f:
                check(json.load(f)["server"] == url, "new address saved to the config")
        finally:
            files.kill()
        print("\nALL OK")
    finally:
        if watcher:
            watcher.kill()
        srv.kill()


if __name__ == "__main__":
    main()
