# telegent-desk

A Claude Code mod for telegent that keeps team mail out of the agent's context. The person at
the terminal sees the mail; the agent gets a short note between turns and reads full text only
through tools it calls itself.

## What it does

- Checks the server every `pollSeconds` (30 by default) through `tg.py inbox --json` and
  `tg.py summary --json`. The mod never sees the token: `tg.py` reads it from its own config.
- Status line: `tg 3 · срочно 1 · ждут 1`, or `tg: нет связи` when the client fails.
- A band above the prompt for the newest urgent or personally addressed message, with
  "Агенту", "ack" and "Скрыть".
- `/tg` opens a pane with every unacknowledged message and the same buttons.
- After each turn the agent receives at most one note: ids, authors, types and urgency of new
  urgent or personally addressed messages and of the messages the person forwarded with
  "Агенту". Each id is named once per session. Message text never goes into the note; the one
  exception is the secretary's one-line retelling of an `action` (below), model-written from
  the message, stripped of quotes and tags, and never part of a wake prompt.
- Tools for the agent: `mcp__telegent-desk__inbox`, `read`, `reply`, `ack`.

## The secretary

With `triage` on (the default) a small model (`secretaryModel`, `haiku` by default) sorts
each new message into `noise`, `fyi`, `action`, `human` or `status-question`:

| label | where it goes |
|---|---|
| `action` | a note to the agent with a one-line retelling by the secretary |
| `human` | a desktop notification to the person; nothing to the agent unless it is urgent |
| `status-question` | a draft reply in the `/tg` pane, sent only by "Отправить" |
| `noise`, `fyi` | the pane only |

The person sees mail at once; a message reaches a note only after it is sorted. A message
that cannot be sorted (secretary off, budget spent, the model refused) follows the plain rule:
urgent or personal mail goes to the agent. Urgent mail reaches the agent under any label
except `status-question`. Drafts come from one tool-less question over the session's own
transcript (`$.model.fork`), so they can say what the agent is doing. The question's own text
is not part of that prompt, only its author: a crafted question cannot steer the fork. Each draft
carries its own idempotency key, and a sent draft acknowledges its question. An open request
answered this way is also marked `done` with a reference to the thread. When closing or
acknowledging fails after the reply went out, the draft stays marked as sent and each poll
retries the remaining steps. A question already acknowledged or answered, or a request
already done or rejected, gets no reply. Urgent mail also raises a desktop notification, once per id;
more than three new ones at once (a session opened over a backlog) raise one summary notice.

`classifyPerHour` (30 by default) caps model calls per hour. The count is kept in the mod's
store as well, so `/clear` and `/resume` do not reset it. Each Claude Code session enforces it
on its own: two sessions running at the same moment can each take the last call of the hour.

## Autopilot (each switch off by default)

- `autoReplyStatus`: drafts for status questions are sent at once, led by "Ответ секретаря агента:".
  Requests, alerts and decisions are excluded: sending would close them, and a label is a small
  model's guess, so their drafts wait for "Отправить" in the pane.
  Before sending, the mod asks the server whether the question is still open: not acknowledged
  and not answered by the agent in its thread. Every session of the agent sends with the key
  `telegent-desk-auto-<id>`, so the server keeps one reply; a failed automatic send is retried
  by the next poll and by "Отправить" with the same key. Any answer of the agent's in the thread
  settles the question: the mod then only closes and acknowledges it.
- `fileGuard`: before Edit, Write or NotebookEdit, the mod checks active telegent claims with the
  key `file:<path relative to the project>`. When someone else holds the file, `ask` turns the
  permission into a question, and `deny` refuses the edit with a reason that names the holder.
  In `auto` and `acceptEdits` permission modes an `ask` is decided by the mode, not by the
  person, so only `deny` stops the edit there. The claim's own text never reaches the model.
  The guard asks the server before each guarded edit and waits at most 5 s. When the server
  does not answer, it uses the last list the server did return, so an outage does not free held
  files; while polls fail, it uses that list at once without asking. It reads up to 1000 active claims, the server's maximum. A path is checked as spelled
  and, through symbolic links, where it really lands. Edits through Bash (`sed -i`, redirects)
  are not tool edits and pass the guard.
- `boardAuto`: after a turn, at most every 10 minutes, the secretary turns the agent's answer
  into one line for `board set --now`. It counts against `classifyPerHour`.
- `wakeIdle`: when mail for the agent arrives while the session is idle, the note is submitted
  as a plugin prompt (framed as the plugin's, not the person's words) and starts a turn. Mail
  already waiting when the session opens never wakes it; it goes into the next turn's note. At
  most six wakes an hour per session, so two agents that answer each other stop trading turns.

## Setup

The project must be connected to telegent (`tg.py connect ...` writes `.telegent.json` with
`client_path`). Load the mod with `claude --plugin-dir <path>/mods/telegent-desk`, or install it
from a marketplace that lists it.

A `.telegent.json` found on disk names the script the mod runs, the server and the token file
sent there, and a cloned repository could ship one. So whenever the mod takes anything from a
found config, it runs nothing and asks in the band above the prompt, naming the client, the server
and the token file: "Разрешить" remembers the config path, client, `server`, `token_file` and `url_source`
in the mod's store, "Не сейчас" leaves the session without telegent. A change of any of them
asks again. The mod rechecks the config before every client run, also when only `client` is set
and a `.telegent.json` appears later: a change in the middle of a session stops the client and
asks in the same band, and "Разрешить" resumes it. A version already allowed, in another session
too, is taken without asking. Each run gets the allowed server and token file through
`TELEGENT_URL` and `TELEGENT_TOKEN_FILE`, so `tg.py` sends the token nowhere else and does not look
for a moved server itself; a move that the agent's own hooks find and save into the config is asked
about once. Only `client` and `config` both set in the options run without asking. With only
`client` set, `tg.py` may also take `TELEGENT_CONFIG`, a `tg.config.json` beside the client or
`~/.telegent/config.json`: those are the person's own settings, not a project's, and are not asked
about.

Options (`/config` or `pluginConfigs["telegent-desk"].options` in user settings):

| option | default | meaning |
|---|---|---|
| `python` | `python3` | Python that runs `tg.py` |
| `client` | `client_path` from `.telegent.json` | path to `tg.py` |
| `config` | the nearest `.telegent.json` | telegent config |
| `pollSeconds` | 30 | poll interval, at least 10 |
| `triage` | on | sort, retell and draft with a small model |
| `classifyPerHour` | 30 | model calls the secretary may make in an hour |
| `secretaryModel` | `haiku` | model alias or id for the secretary |
| `autoReplyStatus` | off | send status drafts without a press |
| `fileGuard` | `off` | `off`, `ask` or `deny` for files held by others |
| `boardAuto` | off | keep the board's "now" line current |
| `wakeIdle` | off | start a turn for new mail while idle |

## With the telegent settings hooks

The mod replaces the Claude Code lifecycle hooks that `tg.py connect --client claude` installs.
Running both announces the same ids twice. Remove them with `tg.py claude uninstall` in the
project, or keep the hooks and do not load the mod. Codex keeps using its hooks.

## Development

```
claude plugin validate mods/telegent-desk
claude plugin test mods/telegent-desk
```
