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
    for name, kind, owner in (("alice", "human", "alice"), ("bob", "human", "bob"),
                              ("alice-agent", "agent", "alice"), ("bob-agent", "agent", "bob")):
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
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
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

        with urllib.request.urlopen(url + "/sw.js") as resp:   # no auth: the browser fetches it by itself
            swb = resp.read().decode()
            check(resp.status == 200 and "javascript" in resp.headers["Content-Type"] and "notificationclick" in swb
                  and resp.headers["Service-Worker-Allowed"] == "/", "service worker is served without login")

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
        check("НЕ слушает" not in r.stdout and "Входящие: 1 (#1 request от alice-agent (agent) СРОЧНО [отправлено: " in r.stdout
              and "; доставлено клиенту: " in r.stdout,
              "hook: own watch alive, lists unacked ids")
        check("Пришли" not in r.stdout and "resnet18" not in r.stdout and "model.pt" not in r.stdout,
              "hook never prints message text (peer text must not arrive as the user)")
        r = run(["hook", "--status-only"], B)
        check("Входящие: 1" in r.stdout, "--status-only accepted")
        r = run(["hook", "--session-start"], A)
        check("твой watch НЕ слушает" in r.stdout, "hook warns when own watch is dead")
        r = run(["send", "проверка связи", "--to", "alice"], B)
        check("внимание: alice сейчас не слушает" in r.stdout, "sender warned: recipient not listening")

        r = run(["inbox"], B)
        check("*#1 " in r.stdout, "inbox shows unread #1")
        r = run(["read", "1"], B)
        check("рабочая переписка и просьбы коллег" in r.stdout and "нужны model.pt" in r.stdout, "read full")
        r = run(["inbox"], B)
        check("#1 " in r.stdout and "*#1" not in r.stdout, "read marks read, still in inbox until ack")
        r = run(["sent"], A)
        check(re.search(r"bob-agent: доставлено .*, прочитано \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", r.stdout), "sent shows full read time")

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

        # Message content is not classified or rejected as a secret by the transport.
        r = run(["send", "пароль: Hunter2Hunter2"], A)
        check(r.returncode == 0, "client sends assigned-value examples")
        r = run(["send", "ключ ghp_" + "a" * 36], A)
        check(r.returncode == 0, "client sends token-shaped examples")
        r = run(["send", "зайди так: Sup3rPassw0rdXYZ"], A)
        check(r.returncode == 0, "legacy secret_files does not filter content")
        r = run(["send", "сервер: ssh root@example.net -p 1335, пароль в secrets.txt"], A)
        check("отправлено" in r.stdout, "non-secret lines of secret file and 'пароль в файле' pass")
        req = urllib.request.Request(url + "/api/messages", method="POST",
                                     data=json.dumps({"body": "token=abcdef1234567890"}).encode(),
                                     headers={"Authorization": "Bearer " + open(cfgs and os.path.join(tmp, "alice-agent.token")).read().strip(),
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as response:
            check(response.status == 200, "server accepts code with token assignments")

        # Approvals are accepted from project humans, independent of agent ownership.
        r = run(["send", "запустить обучение на 8 GPU", "-t", "request", "--approval", "-T", "data"], B)
        aid = int(r.stdout.split()[0][1:])
        r = run(["approve", str(aid)], A, ok=False)
        check(r.returncode == 2 and "только человек" in r.stderr, "agent cannot approve")
        run(["approve", str(aid), "--note", "ок, лей"], HY)
        r = run(["read", str(aid)], A)
        check("ОДОБРЕНО bob" in r.stdout and "НЕ твой человек" not in r.stdout, "approval by project human applies")
        run(["approve", str(aid)], H)
        r = run(["read", str(aid)], A)
        check("ОДОБРЕНО alice" in r.stdout, "approval by own human")
        r = run(["send", "арендовать сервер на час?", "-t", "decision"], A)
        did = int(r.stdout.split()[0][1:])
        run(["deny", str(did), "--note", "не сегодня"], H)
        r = run(["read", str(did)], A)
        check("статус: rejected" in r.stdout and "ОТКАЗАНО alice" in r.stdout, "decision denied -> rejected")

        # approval addressed to one person
        r = run(["send", "закупить 2 часа аренды", "-t", "request", "--approver", "alice", "--to", "alice-agent"], B)
        pid_ = int(r.stdout.split()[0][1:])
        r = run(["read", str(pid_), "--no-mark"], A)
        check("ждёт решения alice" in r.stdout, "approver: read shows who decides")
        r = run(["approve", str(pid_)], HY, ok=False)
        check(r.returncode != 0 and "принимает только alice" in r.stderr, "approver: another human cannot decide")

        def api_get(path, who):
            tok = open(os.path.join(tmp, who + ".token")).read().strip()
            return json.loads(urllib.request.urlopen(urllib.request.Request(
                url + path, headers={"Authorization": "Bearer " + tok})).read())["messages"]
        mine_a = [m["id"] for m in api_get("/api/messages?pending_approval=mine", "alice")]
        mine_b = [m["id"] for m in api_get("/api/messages?pending_approval=mine", "bob")]
        check(pid_ in mine_a and pid_ not in mine_b, "pending_approval=mine: only for the approver")
        check(pid_ in [m["id"] for m in api_get("/api/messages?pending_approval=1", "bob")],
              "pending_approval=1 still lists it for everyone")
        m = [x for x in api_get("/api/messages?pending_approval=1", "bob") if x["id"] == pid_][0]
        check(m["approver"] == "alice" and "alice" in m["to"] and m["needs_approval"], "approver in JSON, added to recipients")
        run(["approve", str(pid_)], H)
        r = run(["read", str(pid_), "--no-mark"], A)
        check("ОДОБРЕНО alice" in r.stdout, "approver: decision by the approver")
        check(pid_ not in [m["id"] for m in api_get("/api/messages?pending_approval=mine", "alice")],
              "pending_approval=mine: decided message is gone")
        r = run(["send", "кто решит?", "--approver", "bob-agent"], A, ok=False)
        check(r.returncode != 0 and "одобряющий должен быть человеком" in r.stderr, "approver must not be an agent")
        r = run(["send", "кто решит?", "--approver", "nobody"], A, ok=False)
        check(r.returncode != 0 and "одобряющий должен быть человеком" in r.stderr, "approver must be in the project")

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

        # claim lifetime: expiry, takeover of an expired claim, extension, reminders
        r = run(["claim", "exp42 short", "--hours", "0.0003"], A)
        r = run(["claim", "exp42 live"], A)
        r = run(["claims"], B)
        check("ещё " in r.stdout and "ПРОСРОЧЕНА" not in r.stdout, "claims: live claim shows remaining time")
        time.sleep(1.6)
        r = run(["claims"], B)
        check("exp42 short" in r.stdout and "ПРОСРОЧЕНА" in r.stdout and "назад" in r.stdout,
              "expired claim is still listed, marked as expired")
        r = run(["hook"], A)
        check("просроченных заявок: 1" in r.stdout and "exp42" not in r.stdout, "hook: count of expired claims, no claim text")
        r = run(["inbox", "--all", "--limit", "1"], A)
        check("просроченных заявок: 1" in r.stdout, "inbox footer: expired claims reminder")
        r = run(["claim", "exp42 live"], B, ok=False)
        check(r.returncode == 2 and "alice-agent" in r.stderr, "live foreign claim still conflicts")
        r = run(["claim", "exp42 short", "--hours", "2"], A)
        check("обновлено" in r.stdout, "same holder re-claim extends")
        r = run(["claims"], B)
        check("ПРОСРОЧЕНА" not in r.stdout and len([ln for ln in r.stdout.splitlines() if "exp42 short" in ln]) == 1,
              "extended claim is live again, no duplicate")
        r = run(["hook"], A)
        check("просроченных" not in r.stdout, "hook: no reminder after extending")
        run(["claim", "exp42 gone", "--hours", "0.0003"], A)
        time.sleep(1.6)
        r = run(["claim", "exp42 gone"], B)
        check("занято: exp42 gone" in r.stdout and "просроченной заявки alice-agent" in r.stdout,
              "another agent takes over an expired claim without --force")
        r = run(["claims", "--all"], B)
        check("истекла" in r.stdout, "expired claim released with the note")
        # an old claim without expiry (pre-migration row) never expires
        import sqlite3
        con = sqlite3.connect(db)
        con.execute("UPDATE claims SET expires = NULL WHERE key = 'exp42 live'")
        con.commit()
        con.close()
        r = run(["claims"], B)
        check("exp42 live" in r.stdout and "ПРОСРОЧЕНА" not in r.stdout, "claim without expiry still works")
        r = run(["claim", "exp42 live"], B, ok=False)
        check(r.returncode == 2, "claim without expiry still conflicts")

        # board: age, back-at hint for senders
        r = run(["board", "set", "--back", "~23:00"], A)
        check("вернётся: ~23:00" in r.stdout and "обновлено" in r.stdout and "давно" not in r.stdout, "board shows back and age")
        r = run(["send", "ты тут?", "--to", "alice-agent"], B)
        check("По доске: вернётся ~23:00 (запись " in r.stdout, "not-listening warning carries the board back-at")
        run(["board", "set", "--back", ""], A)
        r = run(["send", "ты тут?", "--to", "alice-agent"], B)
        check("По доске: tuning 16k (запись " in r.stdout, "warning falls back to the board 'now' text")
        con = sqlite3.connect(db)
        con.execute("UPDATE board SET ts = ts - 60 * 3600 WHERE name = 'alice-agent'")
        con.commit()
        con.close()
        r = run(["send", "ты тут?", "--to", "alice-agent"], B)
        check("не слушает" in r.stdout and "По доске" not in r.stdout, "stale board entry (>48 h) adds nothing")
        r = run(["board"], B)
        check("давно не обновлялась" in r.stdout, "board marks entries older than 24 h")

        # reminder about what waits for me: explicit questions/requests, unanswered or not yet taken
        def waiting_n(out):
            m_ = re.search(r"ждущих твоего ответа: (\d+)", out)
            return int(m_.group(1)) if m_ else 0
        base = waiting_n(run(["hook"], B).stdout)
        r = run(["send", "Вопрос-zq про веса", "-t", "question", "--to", "bob-agent"], A)
        qid = int(re.match(r"#(\d+)", r.stdout).group(1))
        r = run(["send", "Вопрос-zq всем", "-t", "question"], A)       # to everyone: not counted
        run(["send", "Просьба-zq всем", "-t", "request"], A)
        r = run(["hook"], B)
        check(waiting_n(r.stdout) == base + 1 and f"#{qid} question от alice-agent" in r.stdout
              and "zq" not in r.stdout and "[telegent]" in r.stdout, "hook: waiting question counted with id/type/author, no text")
        r = run(["inbox"], B)
        check(waiting_n(r.stdout) == base + 1 and f"#{qid} question от alice-agent" in r.stdout and "zq" not in r.stdout.split("-- ")[-1],
              "inbox footer: waiting item")
        r = run(["inbox", "--all", "--limit", "1", "--type", "info"], B)
        check(waiting_n(r.stdout) == base + 1, "inbox footer also under a filter")
        wl = [m["id"] for m in api_get("/api/messages?waiting_me=1&limit=100", "bob-agent")]
        check(qid in wl and len(wl) == base + 1, "waiting_me=1 list filter returns exactly the waiting messages")
        run(["ack", str(qid)], B)
        check(waiting_n(run(["hook"], B).stdout) == base + 1, "ack alone does not answer a question")
        run(["reply", str(qid), "ответ"], B)
        check(waiting_n(run(["hook"], B).stdout) == base, "reply in the thread removes the question from the waiting list")
        check(qid not in [m["id"] for m in api_get("/api/messages?waiting_me=1&limit=100", "bob-agent")],
              "waiting_me=1 list drops the answered question")
        r = run(["send", "Просьба-zq лично", "-t", "request", "--to", "bob-agent"], A)
        rid = int(re.match(r"#(\d+)", r.stdout).group(1))
        check(waiting_n(run(["hook"], B).stdout) == base + 1, "open request addressed to me counts")
        run(["take", str(rid)], B)
        r = run(["hook"], B)
        check(waiting_n(r.stdout) == base and re.search(rf"взято тобой и не закрыто: 1 \(#{rid}\)", r.stdout),
              "taken request moves to 'taken and not closed'")
        check(re.search(rf"взято тобой и не закрыто: 1 \(#{rid}\)", run(["inbox"], B).stdout), "inbox footer: taken and not closed")
        run(["done", str(rid)], B)
        check("взято тобой" not in run(["hook"], B).stdout, "done request leaves 'taken and not closed'")

        # watch_cmd in config: the hook suggests it literally instead of the auto-built command
        r = run(["hook"], A)
        check("Запусти на Monitor:" in r.stdout and "tg.py" in r.stdout and "watch" in r.stdout
              and A in r.stdout, "hook default watch command keeps this account's config")
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
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
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
        check(code == 401, "agents cannot log in with a password or expose their existence through login")
        code, _ = http("POST", "/api/login", {"name": "alice", "password": "wrong password"})
        check(code == 401, "wrong password rejected")
        code, d = http("POST", "/api/login", {"name": "alice", "password": "correct horse 1"})
        sess = d.get("token", "")
        check(code == 200 and sess.startswith("tgs_"), "login returns a session token")
        code, d = http("GET", "/api/me", tok=sess)
        check(code == 200 and d["name"] == "alice" and d["session"], "session token works like a token")
        r = run(["send", "пример сессии tgs_" + 'a'*48], A)
        check(r.returncode == 0, "client sends session-token-shaped examples")
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
        code, d = http("POST", "/api/invites", {"name": "alice-agent", "kind": "agent"}, human_tok, "hackathon-2")
        check(code == 200 and d["purpose"] == "new", "the same agent name is available in another project")
        code, d = http("POST", "/api/invites", {"name": "alice-agent", "kind": "agent", "account_id": "alice-agent"},
                       human_tok, "hackathon-2")
        check(code == 404, "a foreign account id cannot move the existing agent into another project")
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
        # one text per message: no separate subject any more
        r = run(["send", "второй абзац", "-s", "первая строка", "--to", "bob-agent"], A)
        mid = int(r.stdout.split()[0][1:])
        r = run(["read", str(mid), "--no-mark"], B)
        check("первая строка\nвторой абзац" in r.stdout and "тема:" not in r.stdout, "-s becomes the first line of the text")
        long_line = "длинная строка " * 15
        run(["send", long_line, "--to", "bob-agent"], A)
        r = run(["inbox", "--limit", "3"], B)
        last = [ln for ln in r.stdout.splitlines() if "длинная строка" in ln][-1]
        check(" | " not in last, "inbox lines show the text once, even with a long first line")
        # a message stored the old way (separate subject) is merged when the server starts
        srv.kill()
        srv.wait()
        con = sqlite3.connect(db)
        pid = con.execute("SELECT id FROM projects WHERE slug = 'main'").fetchone()[0]
        old = con.execute("INSERT INTO messages(ts, author, author_kind, owner, host, recipients, topic, type, priority,"
                          " subject, body, project_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (time.time(), "alice-agent", "agent", "alice", "x", "", "", "info", "normal",
                           "старая тема", "старый текст", pid)).lastrowid
        con.commit()
        con.close()
        srv = subprocess.Popen([PY, os.path.join(ROOT, "server.py"), "--db", db, "serve", "--port", str(port)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
        time.sleep(1.2)
        r = run(["read", str(old), "--no-mark"], B)
        check("старая тема\nстарый текст" in r.stdout, "an old subject is merged into the text")
        def stored_body():
            c = sqlite3.connect(db)
            b = c.execute("SELECT body FROM messages WHERE id = ?", (old,)).fetchone()[0]
            c.close()
            return b
        first = stored_body()
        srv.kill()
        srv.wait()
        srv = subprocess.Popen([PY, os.path.join(ROOT, "server.py"), "--db", db, "serve", "--port", str(port)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
        time.sleep(1.2)
        check(stored_body() == first and first.count("старая тема") == 1, "merging happens once")
        # renaming: a person renames themselves and their own agents; history and logins follow
        human_tok = open(os.path.join(tmp, "alice.token")).read().strip()
        bob_tok = open(os.path.join(tmp, "bob.token")).read().strip()
        agent_tok = open(os.path.join(tmp, "alice-agent.token")).read().strip()

        def http(method, path, body=None, tok=None):
            req = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", "X-Telegent-Project": "main",
                                                  **({"Authorization": "Bearer " + tok} if tok else {})})
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")
        check(http("POST", "/api/rename", {"name": "alice-agent", "new": "ally"}, bob_tok)[0] == 403,
              "nobody renames someone else's agent")
        check(http("POST", "/api/rename", {"name": "alice-agent", "new": "x"}, agent_tok)[0] == 403,
              "agents do not rename")
        check(http("POST", "/api/rename", {"name": "alice-agent", "new": "Bob"}, human_tok)[0] == 409,
              "a taken name (in any case) is refused")
        check(http("POST", "/api/rename", {"name": "alice-agent", "new": "bad name!"}, human_tok)[0] == 400,
              "an invalid name is refused")
        code, _ = http("POST", "/api/rename", {"name": "alice-agent", "new": "ally-bot"}, human_tok)
        check(code == 200, "a person renames their agent")
        r = run(["whoami"], A)
        check(r.stdout.startswith("ally-bot (agent, владелец alice)"), "the agent's token keeps working under the new name")
        r = run(["send", "по новому имени", "--to", "ally-bot"], B)
        check("отправлено" in r.stdout, "the agent is addressable by the new name")
        r = run(["inbox", "--all", "--limit", "500"], B)
        check("ally-bot" in r.stdout and "alice-agent(" not in r.stdout, "history shows the new name")
        check("теперь называется ally-bot" in r.stdout, "the project is told about the rename")
        code, _ = http("POST", "/api/rename", {"new": "alicia"}, human_tok)
        check(code == 200, "a person renames themselves")
        code, d = http("GET", "/api/me", tok=human_tok)
        check(d["name"] == "alicia" and d["is_admin"], "the person's token keeps working, rights stay")
        r = run(["whoami"], A)
        check("владелец alicia" in r.stdout, "their agents now belong to the new name")
        http("POST", "/api/password", {"new": "renamed pass 1"}, human_tok)
        check(http("POST", "/api/login", {"name": "alicia", "password": "renamed pass 1"})[0] == 200 and
              http("POST", "/api/login", {"name": "alice", "password": "renamed pass 1"})[0] == 401,
              "login goes by the new name, the old one no longer works")
        # adding people one already works with, and deleting a project
        def hp(method, path, body=None, tok=None, project=None):
            req = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json",
                                                  **({"X-Telegent-Project": project} if project else {}),
                                                  **({"Authorization": "Bearer " + tok} if tok else {})})
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")
        code, d = hp("POST", "/api/projects", {"title": "Trio"}, human_tok)
        check(code == 200 and d["project"]["slug"] == "trio", "a new project for the next checks")
        code, d = hp("GET", "/api/members/candidates", tok=human_tok, project="trio")
        names = {c["name"] for c in d["candidates"]}
        check("bob" in names and "carol" not in names, f"people from shared projects are offered ({sorted(names)})")
        check(hp("POST", "/api/members/add", {"name": "carol"}, human_tok, "trio")[0] == 403,
              "someone without a shared project needs a link")
        code, _ = hp("POST", "/api/members/add", {"name": "bob"}, human_tok, "trio")
        check(code == 200 and "trio" in [x["slug"] for x in hp("GET", "/api/projects", tok=bob_tok)[1]["projects"]],
              "a shared-project person is added in one click")
        check(hp("POST", "/api/members/add", {"name": "masha"}, bob_tok, "trio")[0] == 403, "only owners add")
        code, d = hp("POST", "/api/invites", {"name": "MASHA"}, human_tok, "trio")
        check(code == 200 and d["purpose"] == "add" and d["name"] == "masha", "invites match names without case")
        code, d = hp("POST", "/api/invites", {"name": "guessed-name"}, human_tok, "trio")
        check(d["purpose"] == "new", "an unknown name gets a new-person invite")
        code, j = hp("POST", "/api/join", {"code": d["code"], "as_existing": True}, carol)
        check(code == 200 and j["name"] == "carol" and j["project"] == "trio",
              "someone with an account takes a new-person invite as themselves")
        check(hp("GET", "/api/me", tok=carol, project="trio")[0] == 200, "and is in the project")
        check(not any(u["name"] == "guessed-name" for u in hp("GET", "/api/me", tok=human_tok, project="trio")[1]["users"]),
              "no second account appeared")
        code, d = hp("POST", "/api/messages", {"body": "в trio"}, human_tok, "trio")
        trio_msg = d["id"]
        check(hp("POST", "/api/project/delete", {"confirm": "trio"}, bob_tok, "trio")[0] == 403, "only owners delete")
        check(hp("POST", "/api/project/delete", {"confirm": "wrong"}, human_tok, "trio")[0] == 400,
              "deleting needs the project's name typed")
        code, d = hp("POST", "/api/project/delete", {"confirm": "Trio"}, human_tok, "trio")
        check(code == 200 and d["deleted"] == "trio", "the owner deletes the project")
        check("trio" not in [x["slug"] for x in hp("GET", "/api/projects", tok=bob_tok)[1]["projects"]],
              "members lose the project")
        check(hp("GET", f"/api/messages/{trio_msg}", tok=human_tok)[0] == 404, "its messages are gone")
        print("\nALL OK")
    finally:
        if watcher:
            watcher.kill()
        srv.kill()


if __name__ == "__main__":
    main()
