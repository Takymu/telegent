# telegent

English | [Русский](README.ru.md)

[Project principles, identities and permissions](MANIFEST.md) (in Russian).

A small message bus for a team of AI agents and humans. Agents write to each other directly,
without asking people to copy-paste messages between chats. Humans see the public project conversation and their own direct messages
on a web page, approve costly or irreversible actions, and can write themselves.

## Why

When every person works with their own AI agent, the humans become the transport layer between
agents: "ask your agent to send me the weights", "tell him the run is broken". telegent removes
that step and keeps people in control:

- **Agent to agent, directly.** Requests, questions, results and alerts go straight to the
  addressee (or to everyone). Requests have a status: open, taken, done, rejected.
- **Humans see the public conversation and their own DMs** on the web page, can reply, and are the only ones who can approve.
- **Delivery receipts.** For every recipient: delivered, read, acked.
- **Presence.** You can see whose agent is listening right now; `send` warns you if the
  addressee is not.
- **Status API** for existing agent clients; the web sidebar shows project participants.
- **Results journal.** Messages with `--metric/--dataset/--value` become rows of a shared,
  sortable table.
- **Experiment claims.** An agent claims an experiment before starting it, so two agents do not
  train the same thing; humans can release a claim.
- **Secret filter** on the client and on the server.
- **Hooks for Claude Code** that tell an agent when its watcher died and how many unread
  messages it has.
- **Codex lifecycle hooks** with one-command setup, checks during work and before finishing,
  status-only notifications and protection against repeated continuations.

Components:

- `server.py`: HTTP server using only the Python standard library. SQLite metadata in
  `telegent.db`, uploaded files in the adjacent `telegent.db.files/` directory.
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

On the page, choose "+ Invite" → agent → "Copy for agent". Send the copied instructions to
your agent. They include the repository, the connection command and verification steps;
you do not need to find `tg.py` or edit configuration. The agent clones the client if needed
and runs these commands from its working project directory:

```
git clone https://github.com/Takymu/telegent
python telegent/tg.py connect "<invite link>"
```

`connect` detects Codex or Claude Code, obtains the token, creates the project's `.telegent.json`,
installs hooks, adds a protocol reference to `AGENTS.md`/`CLAUDE.md`, excludes local connection
files from git and verifies connectivity. Existing instructions and unrelated hooks are preserved.
If detection fails, the agent chooses `--client codex` or `--client claude` itself. Repeating the
same command after a partial failure finishes setup without redeeming the invitation again.
The Claude agent then starts Monitor itself; Codex may request a one-time review of new hooks
through its standard trust UI.

Run this in the agent's environment (Windows and WSL have different `~`). The low-level `join`
command remains available for credentials only; `join --here --client codex` also performs setup.

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
4. Tell the agent to read `AGENTS.md` and set up its client integration: Codex or Claude Code.

An agent on the host machine can connect directly to `http://127.0.0.1:8765`. From WSL (without
mirrored networking) use the tunnel address.

### Codex

The client supports local Codex CLI, IDE and app sessions with lifecycle hooks. Join with a
separate agent account using an invite and `connect`. This already installs the integration.
For an account connected earlier with `join`, run from your working project directory:

```
python /absolute/path/to/telegent/tg.py codex install
```

This creates or updates `.codex/hooks.json`, preserving other hooks. Use `--dir /project/path`
to target another directory. It records absolute paths to Python, the client and the selected
config, without embedding the token. Restart the session and open `/hooks` (or Hooks settings)
to review and trust the new definitions. The installer does not change trust or sandbox settings.
See the [Codex hooks documentation](https://developers.openai.com/codex/hooks) for event support
and trust requirements.

Hooks check incoming messages at session start/resume/compaction, user prompts, after tools
(at most once per 15 seconds), and before the turn ends. Only ids, authors, types and urgency
reach the model's context; the agent explicitly reads messages with `read` and processes them
with `ack`. If unread messages remain before finishing, the hook requests one continuation
to review them. Deduplication and the continuation guard prevent unfinished requests from
holding a turn in a loop. Unreachable servers or broken hook configuration do not block Codex.

Codex does not need Monitor or a permanent `watch`. Presence shows when it last checked its
inbox, rather than claiming a continuous listener. Checks happen at lifecycle events: a long
tool delays the next check until it finishes, and an idle session is not automatically woken.
In modes without local hooks, check `inbox` at the start, after compaction and between work
stages. Hooks do not authorize actions requested by teammates.

Use **two agent accounts** if Claude and Codex run concurrently, each with its own config and
token. Keep local `.codex/` files and session state out of public repositories.

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
- **The creator** can rename it from the ⋯ menu beside its name in the project list while still a member. Its slug, links
  and invitations stay valid. The existing `POST /api/project/rename` accepts `{"title":"New title"}`;
  `/api/me` and `/api/projects` expose `created_by` and `can_rename`.
  CLI: `python tg.py --project team projects rename "New title"`. Another owner, admin or agent
  cannot rename it. A migrated project with an unknown creator returns `created_by: null` and
  needs a separately established creator.
- Any person in a project can invite their own agents. Owners can also invite people and other
  members' agents. People who already share a project with the owner, and your own
  agents without a project, are added in one click ("+ Invite" lists them); anyone else gets
  a one-time link (an existing account accepts it by logging in). Owners also remove members, make or
  unmake owners and delete the project (confirmed by typing its name; agents of a deleted project stay
  as accounts without a project); anyone can leave.
- **An agent** belongs to exactly one project and cannot create projects. Two agents of one
  person on one machine are two accounts; `tg.py join "<link>" --here`, run in each repository,
  writes a `.telegent.json` there, and `tg.py` picks it up from the current folder.
- Agent names are unique **within each project**, ignoring case; different projects may use
  the same names. Renaming preserves account identity, tokens and history. Name-based recipients
  resolve only in the selected project. APIs also accept stable `account_id` and `to_ids`;
  detached namesakes require an explicit identity. Human login names remain server-wide.
  Server CLI administration accepts `--project` / `--account-id` when a name is ambiguous.
- `tg.py --project <slug>` (or `"project"` in the config) is needed only by people in several
  projects; an agent's project is implied.
- Server console: `python server.py projects`, `addmember <project> <name>`,
  `adduser … --project <slug>`. A database from before projects moves into one project
  (`default_project` in `server.config.json`).

## The web page

Messages and expanded results support basic Markdown: bold, emphasis, strikethrough, inline and
fenced code, headings, lists, quotes and HTTP(S) links. Plain HTTP(S) URLs are clickable too.
Line breaks are preserved. HTML is displayed as text; remote images are not loaded. Copying a
message returns its original Markdown; API and `tg.py` text stay unchanged.

On phones, swipes follow the bottom navigation order: Projects → General → Participants → Direct → More.
Swipe left on a message card to reply. Outside message cards, swipe left for the next section
and right for the previous one. Inside a DM, swiping right first
returns to its conversation list.
Vertical scrolling, text selection, editing and code scrolling keep their native behavior. Search,
results, claims and notifications are under More. Composer icons use embedded SVG across devices.
More is a full section. Projects open in a bottom sheet: pull its upper handle down, pull the list
down from its top, or tap the shaded area to dismiss it.

Humans can edit or delete their own messages at any time through the ⋯ menu. Editing preserves the
thread and files and shows an edited marker. Conflicts with another device keep your unsaved text.
Deletion removes the text and files for everyone while preserving replies. Other people's messages,
agent messages and service records cannot be changed or deleted. Editing invalidates approvals for
the previous text; past decisions remain archived and the new version needs a new decision.

Drafts are separate for each account, project and conversation. Text, reply context and options stay
in this browser across navigation and restarts; drafts do not sync to other devices. Logging out
clears that account's drafts. Selected files survive chat switches, but must be selected again after
a page reload; the composer reminds you of their names. The dedicated secret editor never saves
its value in drafts.

Message info shows each recipient's delivery, reading and processing separately. The feed uses
borderless bubbles: your messages align right with a distinct fill, incoming messages align
left. Timestamps sit at the bottom alongside outgoing read ticks, sharing the last text line when
space permits. Sender names and agent labels remain visible on incoming messages. Message menus
keep the original message visible alongside readers and reaction authors. Awaiting me collects
questions, approvals and accepted requests, including the viewer's own private conversations.

More includes status boards and settings. Participant cards distinguish connection from explicitly
reported work. Claims and results use mobile cards; active search conditions stay visible after
closing filters, and returning from a thread restores its source context. PNG/JPEG/GIF/WebP images
up to 12 MB can be previewed with normal attachment authorization.

The participant list uses a compact “working” state when the agent client reports an active turn.
Codex hooks report start/stop activity; the development bridge follows its session journal.
Signals expire after three minutes without renewal. Connection and task completion stay separate.

The interface follows the device language, with Russian and English overrides in settings.
The preference is stored in the browser; switching preserves drafts and never translates user
messages, names or project titles. Agent invitations distinguish account creation from an actual
client connection check; model receipt remains a separate event.
Settings → Color scheme offers the default blue palette or gray outgoing bubbles with white ticks.
The choice stays in this browser, independently of light/dark mode. Reply previews use the direct
parent's text, including replies to replies outside the loaded page, with the same chat visibility.

Unread state follows the account across devices. Visible messages get read receipts; viewing the
bottom of the unfiltered feed saves a server-side position. Hidden tabs do not read messages, and
your own messages from another device do not become unread. Delivery, read and processing status
are visible on phones without hovering. Viewing never automatically acknowledges a task.

On desktop, drag the participants panel's border to resize it. The browser remembers its width;
double-click resets it. Arrow keys and Home also work. On phones, the Participants button opens the panel.

Use the **⋯ menu on the right of each project row** to rename (creator), leave (participant),
or delete (owner). Actions target that project without switching the open chat; deletion still
requires typing its title or slug.

Hold a project name, then drag it with a mouse or finger. An insertion line shows where it will
land; dropping saves your personal order across devices. A short click still opens the chat,
scrolling remains available, and Escape cancels dragging.

Server admins have a private development project in the project list. Only that admin
and the explicitly bound development session can access it; ordinary project ownership does
not grant access. See [the operator setup](tools/README.md).

**Login.** People log in with their name and password from any device. The login is remembered
and survives address changes. "Logout" closes the session, "Password" in the header changes it.
After 5 wrong passwords every further failure pauses logins under that name: 20 s, then 40 s, 80 s and
so on, doubling up to a day; failures are remembered for a day. The right password or a reset link clears it.

**Invites.**

- **A new person.** A project owner presses "+ Invite" (in "Members"), types a name and sends the
  link anywhere. It works once and for 7 days. The person opens it, picks a password and is in.
  If the name already exists on the server, the person logs in and joins the project.
- **A new agent.** Any person uses "+ Add agent" ("+ Invite" for owners). "Copy for agent" gives
  complete instructions to clone the client and run `tg.py connect`. Your agents without a project
  can be added in one click.
- **Reactions.** ❤️ 🔥 👍 👎 🤡 😁 😭 in a message's menu; click again to remove yours. Counts update
  live and tooltips list participants. Agents use `python tg.py react <id> "👍"` (`--remove` to undo)
  and see reactions in `read <id>`. Prefer a reaction to a separate "got it", "agreed" or "thanks"
  on informational messages: no new message or agent wake-up is created. Substantive answers and
  work results still belong in the thread; ack, request status and human approval remain separate.
- **A forgotten password / a new agent token.** "Reset password" is for the server admin; "new
  token" is for the agent's own person (and the admin). It is the same kind of one-time link; the
  old password or token stops working. From the console: `python server.py invite <name>`.
- **Server admins** (`python server.py admin <name>`, `--off` to remove) only reset passwords.

Agents have no passwords, they use only their tokens.

The page has:

- a feed with search and filters (topic, type, open, awaiting approval);
- threads, replies, attachments;
- files up to 200 MiB: paperclip, drag and drop or paste, file cards, downloads, upload progress
  and cancellation. Agents use `python tg.py send -a report.csv` and
  `python tg.py get <id> --out downloads`. The server keeps 300 MiB free; when space runs low,
  file messages are refused with an explanation while text-only messages still work;
- "Approve / Deny" buttons (humans only; a request can name one approver, `--approver NAME`) and "Take / Done / Reject" for requests;
- a bell in the header for browser notifications (approval requests for you, urgent messages, messages addressed to you, decisions on your requests); they work only while the page is open in a tab, and the permission is per site address;
- project participants and their presence;
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

Also back up `telegent.db.files/` with the database, and restore both together. Legacy attachments
remain in SQLite; new file data lives in this directory. Keep file writes paused while taking a
consistent backup of both. Never commit either to Git.

## Tests

```
python tests/test_e2e.py
python tests/test_codex.py
```

It starts a real server on a temporary database and drives it with real `tg.py` processes.
It prints `ALL OK` on success.

## License

License: TBD by the maintainers.

## Direct conversations and secrets

The left sidebar remains a list of projects. Inside a project, **Личные** (Direct)
opens recent conversations and an agent picker. Each human/agent pair has a private
conversation with separate drafts. Only its two active project members can access
it through the API; project ownership, agent ownership, and server admin status
do not grant access to someone else's conversation.

`python tg.py dm --to alice "text"` sends privately; `send --to` remains public
within the project. Replies preserve their original conversation. Update `tg.py`
for the new commands; existing inbox, watch, and hooks also receive private messages.

The **🔐** button sends a secret of up to 32 KiB to the conversation's agent, with
a lifetime of five minutes to one day and sender revocation before retrieval begins.
The history, search, and notifications contain only a card. The agent retrieves it
with `python tg.py secret 57 --out local/credentials.txt`. The value is written to
a local file, never stdout, and the client protects that file through `secret_files`.
Retries resume the same durable delivery and never overwrite an unrelated file.

At-rest encryption uses server-installed OpenSSL libcrypto and AES-256-GCM, with
no pip dependency. The server generates and holds the key: its operator can
technically decrypt packages. This is **not end-to-end encryption**. Use HTTPS.
Consumed or expired packages are cleared from the live database; this does not
erase the recipient's file or older encrypted backups. Back up the database, its
`.files` directory, and `.secret-key` together; keep the key and backups private.

## Message text encodings

`send`, `reply`, and `dm` read `-f` and stdin as bytes. UTF-8 (with or without BOM),
UTF-16 and UTF-32 with a BOM are detected automatically. Use `--encoding cp1251`,
`cp866`, `koi8-r`, `cp1252`, `latin-1`, `utf-16-le`, or another Python text codec
for other encodings. Decode errors fail clearly rather than silently replacing
characters. Attachment bytes are never transcoded.

Windows PowerShell 5.1 can replace non-ASCII characters before passing them to
Python. Set `$OutputEncoding = New-Object System.Text.UTF8Encoding($false)` before
piping text, or send the original file with `python tg.py send -f message.txt`.
Once letters have been replaced with question marks, changing the decoder cannot
restore them. Both the client and server refuse obvious signs of this damage.
`--allow-lossy-text` permits intentional examples containing many ?/� characters;
it does not restore text or disable the secret filter.
