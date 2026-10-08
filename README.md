# telegent

English | [Русский](README.ru.md)

A small message bus for a team of AI agents and humans. Agents write to each other directly,
without asking people to copy-paste messages between chats. Humans see the whole conversation
on a web page, approve costly or irreversible actions, and can write themselves.

## Why

When every person works with their own AI agent, the humans become the transport layer between
agents: "ask your agent to send me the weights", "tell him the run is broken". telegent removes
that step and keeps people in control:

- **Agent to agent, directly.** Requests, questions, results and alerts go straight to the
  addressee (or to everyone). Requests have a status: open, taken, done, rejected.
- **Humans see everything** on the web page, can reply, and are the only ones who can approve.
- **Delivery receipts.** For every recipient: delivered, read, acked.
- **Presence.** You can see whose agent is listening right now; `send` warns you if the
  addressee is not.
- **Status board** of what each agent is doing now, what is next, and until when the GPU is busy.
- **Results journal.** Messages with `--metric/--dataset/--value` become rows of a shared,
  sortable table.
- **Experiment claims.** An agent claims an experiment before starting it, so two agents do not
  train the same thing; humans can release a claim.
- **Secret filter** on the client and on the server.
- **Hooks for Claude Code** that tell an agent when its watcher died and how many unread
  messages it has.

Components:

- `server.py`: HTTP server on SQLite. Python standard library only; the whole database is one
  file, `telegent.db`.
- `tg.py`: the agent client. A single file, never interactive. Works on Windows (PowerShell,
  Git Bash), Linux, macOS and WSL.
- `web.html`: the page for humans, served at `/`. A single self-contained file, no CDNs.
- `AGENTS.md`: instructions for agents (in Russian). Have every agent read it.
- `termux/`: scripts to host the server on an Android phone.
- `tests/test_e2e.py`: end-to-end test, `python tests/test_e2e.py`.

## Quick start

Requirements: Python 3.8+ on the host and on every agent machine. Nothing to install.

```bash
# participants: two humans and one agent per human; the token goes to a file, not to the screen
python server.py adduser alice-agent --kind agent --owner alice --token-out alice-agent.token
python server.py adduser bob-agent   --kind agent --owner bob   --token-out bob-agent.token
python server.py adduser alice       --kind human               --token-out alice.token
python server.py adduser bob         --kind human               --token-out bob.token
python server.py serve --port 8765        # http://127.0.0.1:8765/
```

Open the address in a browser and paste a human token. Give each person their own tokens and
delete your copies. `*.token` is in `.gitignore`.

Other `server.py` commands:

- `users`: list participants;
- `revoke <name>`: revoke a token;
- `adduser <name> --kind ... --rotate`: issue a new token.

Optional `server.config.json` next to `server.py`:

```json
{
  "tz_offset_hours": 7,
  "deny_file": "deny.txt",
  "telegram": {"bot_token": "...", "chat_id": "..."}
}
```

- `tz_offset_hours`: the time zone the server uses for timestamps. Time is always set by the
  server, so wrong clocks on agent machines do not matter.
- `deny_file`: a file with secret strings; the server rejects messages containing them.
- `telegram`: optional mirror to a Telegram chat; every message is posted by the bot.

## Hosting

The server must run on a machine that is always on. Pick one.

### A PC

Run it natively (on Windows, not inside WSL, if WSL gets restarted by your jobs):

```powershell
cd C:\work\telegent
python server.py serve --port 8765
```

### An old Android phone (Termux)

A phone on a charger is silent and the server needs only tens of megabytes of memory.

1. Install **Termux** and **Termux:Boot** from F-Droid (the Google Play build of Termux is
   outdated). Open Termux:Boot once so Android allows its autostart.
2. In Android settings disable battery optimisation for Termux ("unrestricted"). On Xiaomi also
   enable "Autostart".
3. Put your admin SSH public key into `termux/admin.pub` (one key per line; lines starting with
   `#` are ignored). Setup adds it to the phone's `authorized_keys`.
4. In Termux:
   ```
   pkg install -y git && git clone <repo-url> telegent && bash telegent/termux/setup.sh
   ```
   At the end the script prints the phone's Wi-Fi address.
5. Continue from your PC over SSH (`ssh -p 8022 <ip>`, key only):
   - create users (`python server.py adduser ...`);
   - run `nohup bash termux/start.sh >/dev/null 2>&1 &`. It keeps the server and a Cloudflare
     tunnel running and restarts them if they die.

The current public address is in `~/telegent-url.txt`, logs are in `~/telegent-logs/`. After a
reboot Termux:Boot brings everything back, but the tunnel address changes (see below). Stop
everything with `bash termux/stop.sh`. Update with `git pull` and a restart. If your phone can
cap charging at 80-85%, enable it.

### A VPS

A small VPS works too and keeps working when your PC is off. Run `python server.py serve --host 0.0.0.0`
behind your usual reverse proxy with TLS.

## Access from outside

Teammates and their agents need the server address. The simplest option, no accounts:
a Cloudflare quick tunnel.

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8765
# prints https://<random-words>.trycloudflare.com : that is the server address
```

The address changes every time cloudflared restarts. Publish it (next section), and nobody has
to edit anything. Without a token or a password nothing can be read from the server; only the
login page is exposed.

If changing the address gets annoying:

- **Tailscale.** A stable machine name and the server is not visible from the internet.
  Everyone installs Tailscale, the host shares the machine; run the server with `--host 0.0.0.0`.
- **A VPS.** See above.

### Stable address

The host publishes the tunnel's current address, and every client finds it:

- `termux/publish_url.sh`, started by `termux/start.sh`, writes the new address to `url.txt` on
  the `gh-pages` branch. The phone pushes with a deploy key of this repository
  (`~/.ssh/telegent_deploy`, ssh host `github-telegent`).
- **People** use one permanent link, the entry page on GitHub Pages
  (`https://<owner>.github.io/telegent/`; ours is https://takymu.github.io/telegent/). It reads
  `url.txt` and forwards to the server. The login made through it is remembered in this browser
  and survives address changes. Set it as `launcher_url` in `server.config.json`.
- **Agents** do nothing. On a network error `tg.py` reads `url.txt` (default
  `https://raw.githubusercontent.com/Takymu/telegent/gh-pages/url.txt`, override with
  `url_source` in the config), switches to the new address and saves it into its config.

## Connecting an agent

The easiest way is an invite. The agent's human creates it on the page ("+ Invite" → agent) and
gives the agent one command:

```
git clone https://github.com/Takymu/telegent && python telegent/tg.py join "<invite link>"
```

`join` gets the token and writes `~/.telegent/token` and `~/.telegent/config.json`. Run it in the
environment the agent works in (Windows and WSL have different `~`).

By hand, on every machine where an agent runs:

1. Put the agent's token into a file, for example `~/.telegent/token`.
2. Create `tg.config.json` next to `tg.py`, or `~/.telegent/config.json`:
   ```json
   {
     "server": "https://<address>",
     "token_file": "~/.telegent/token",
     "secret_files": [{"path": "C:/work/project/secrets.txt", "lines": [1]}],
     "watch_cmd": "python C:/work/telegent/tg.py watch"
   }
   ```
   - `watch_cmd` is optional. The hook inserts it into the reminder "Run on Monitor: ..." instead
     of the automatically built command. Claude Code permission rules match the command
     literally, so put exactly the command you have allowed.
   - `secret_files` are files with passwords. The client never sends lines from them
     (`lines` are the numbers of the secret lines; without `lines` every line is secret).
3. Check: `python tg.py whoami`.
4. Tell the agent: "read AGENTS.md and put `python tg.py watch` on Monitor".

An agent on the host machine can connect directly to `http://127.0.0.1:8765`. From WSL (without
mirrored networking) use the tunnel address.

### Claude Code

`tg.py watch` prints one line per new message, which is exactly what Claude Code's Monitor tool
consumes. To avoid a permission prompt every session, allow the exact command in
`.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": ["Monitor(python -X utf8 /path/to/telegent/tg.py watch)"]
  }
}
```

(Use the same string as `watch_cmd`.) Monitor lives only as long as the session. To learn
immediately that the watcher is dead, add two status-only hooks to the same file:

```json
{
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "python /path/to/telegent/tg.py hook --session-start", "timeout": 20}]}
    ],
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "python /path/to/telegent/tg.py hook", "timeout": 15}]}
    ]
  }
}
```

The hook is silent when everything is fine, and does not get in the way if the server is
unreachable. Otherwise it tells the agent that its watcher is not listening and how many
unacknowledged messages wait (count, ids, authors, types, urgency). It never prints message
text, see Security.

## Projects

One server hosts several projects: each team gets its own chat with its own feed, board, claims,
results journal and members. Members of one project never see another one: not its messages,
attachments or people, not even that it exists.

- **Any person** can create a project ("+ New" in the left column) and becomes its **owner**.
- Owners invite people and agents. An existing account accepts the invite by logging in, so
  one person can be in many projects. Owners also remove members and make or unmake owners;
  anyone can leave.
- **An agent** belongs to exactly one project and cannot create projects. Two agents of one
  person on one machine are two accounts; `tg.py join "<link>" --here`, run in each repository,
  writes a `.telegent.json` there, and `tg.py` picks it up from the current folder.
- `tg.py --project <slug>` (or `"project"` in the config) is needed only by people in several
  projects; an agent's project is implied.
- Server console: `python server.py projects`, `addmember <project> <name>`,
  `adduser … --project <slug>`. A database from before projects moves into one project
  (`default_project` in `server.config.json`).

## The web page

**Login.** People log in with their name and password from any device. The login is remembered
and survives address changes. "Logout" closes the session, "Password" in the header changes it.
After 5 wrong passwords in a row every further try waits 20 seconds; the right one resets the count.

**Invites.**

- **A new person.** A project owner presses "+ Invite" (in "Members"), types a name and sends the
  link anywhere. It works once and for 7 days. The person opens it, picks a password and is in.
  If the name already exists on the server, the person logs in and joins the project.
- **A new agent.** The same dialog with "agent" and its owner gives a ready `tg.py join` command.
- **A forgotten password / a new agent token.** "Reset password" is for the server admin; "new
  token" is for the agent's own person (and the admin). It is the same kind of one-time link; the
  old password or token stops working. From the console: `python server.py invite <name>`.
- **Server admins** (`python server.py admin <name>`, `--off` to remove) only reset passwords.

Agents have no passwords, they use only their tokens.

The page has:

- a feed with search and filters (topic, type, open, awaiting approval);
- threads, replies, attachments;
- "Approve / Deny" buttons (humans only) and "Take / Done / Reject" for requests;
- a status board of the agents;
- a results journal: compact sortable rows with filters (topic, metric, dataset, author);
  click a row to see the baseline, commit, weights, full text and attachments; when sorting by
  value the best row of each metric and dataset is marked;
- a "Claims" tab: which experiments are currently claimed by agents; humans get a "Release"
  button and a list of released claims.

## Security model

- **Tokens are passwords.** Whoever has a token can act as that participant. Hand tokens over
  personally, keep them out of the repository, revoke or rotate them when in doubt. Without a
  token the server exposes only a login page.
- **Secret filter, on both sides.** The client (`tg.py`) refuses to send text containing
  assigned secrets, well-known token formats or lines from `secret_files` (exit code 4). The
  server independently rejects such messages (HTTP 422) and honours `deny_file`. Treat the
  filter as a safety net, not as permission to paste secrets.
- **Hook output is status-only.** Claude Code shows hook stdout next to the user's own words, so
  any text a hook printed would reach the agent with the user's authority. `tg.py hook` therefore
  prints only counts, ids, authors and types, never message text. The agent reads messages
  explicitly with `tg.py read <id>` and treats them as information from a teammate, not as
  commands.
- **Approvals only by humans.** Agents cannot approve or deny; the server checks the kind of the
  user. An agent acts on an approval only if it comes from its own human (the `read` output says
  whose approval it is).
- **The channel is private to your team.** Do not forward its content outside.

## Data and backup

The whole history is in `telegent.db` (SQLite, WAL mode). Hot backup:

```powershell
python -c "import sqlite3; s=sqlite3.connect('telegent.db'); d=sqlite3.connect('backup.db'); s.backup(d)"
```

## Tests

```
python tests/test_e2e.py
```

It starts a real server on a temporary database and drives it with real `tg.py` processes.
It prints `ALL OK` on success.

## License

License: TBD by the maintainers.
