# harness-telegram-bridge

Drive a coding agent from Telegram. Send a message from your phone, an agent session on your machine picks it up, works, and replies in the chat — with file attachments, emoji reactions, live-edited progress messages, and tap-to-approve permission prompts.

Runtime-agnostic: the same bot can front **Claude Code**, **Kimi Code**, **OpenCode**, or a blank context-free session. One machine can run several bots at once, each fully isolated.

---

## How it works

Three pieces. Two are in this repo.

```
   Telegram
       │  long-polling
       ▼
┌──────────────────────┐   systemd user unit, one per bot
│  server.ts           │   telegram-mcp@<bot>.service
│                      │
│  · polls Telegram    │   HTTP on $TELEGRAM_MCP_PORT:
│  · runs the gate     │     /mcp         MCP transport
│  · queues inbound    │     /poll        JSON drain for proxy
│  · owns permissions  │     /permission  POST from the proxy
└──────────┬───────────┘
           │  /poll  ·  queues up to 500 messages while nothing is attached
           ▼
┌──────────────────────┐   spawned by the agent as a stdio MCP server
│  proxy.ts            │
│                      │
│  · implements tools  │   reply · react · download_attachment
│  · holds the pin     │   edit_message · wait_for_message
│  · forwards perms    │   one session per bot at a time
└──────────┬───────────┘
           │  stdio MCP
           ▼
    Claude Code / Kimi Code / OpenCode
```

**The poller outlives your session.** `server.ts` runs as an always-on systemd unit. Close your terminal, reboot your laptop, `/clear` your session — the bot stays up and keeps accepting messages. Anything that arrives while no session is attached is queued (up to 500) and flushed the moment one reconnects. You are never the reason a message is lost.

**The pin stops the crossfire.** Telegram allows exactly one poller per token; a second one gets a permanent 409. `proxy.ts` takes a lockfile (`pinned.lock`) so only one agent session owns a given bot. Stale locks from dead processes are detected and cleared automatically, so a crashed session doesn't strand the bot.

---

## Setup

### Prerequisites

- [Bun](https://bun.sh)
- A bot token from [@BotFather](https://t.me/BotFather)
- At least one agent runtime: [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Kimi Code](https://moonshotai.github.io/kimi-code/), and/or [OpenCode](https://opencode.ai)
- Python 3.10+ with `python-telegram-bot` (for daemon.py backends)

### Install

```bash
git clone https://github.com/Swigler/harness-telegram-bridge ~/.claude/telegram-server
cd ~/.claude/telegram-server
bun install

mkdir -p ~/.config/systemd/user
ln -s ~/.claude/telegram-server/telegram-mcp@.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

Then put the launcher on your `PATH` and the runtimes where `tg` looks for them:

```bash
mkdir -p ~/bin ~/.claude/channels/telegram
ln -s ~/.claude/telegram-server/tg ~/bin/tg
ln -s ~/.claude/telegram-server/runtimes ~/.claude/channels/telegram/runtimes
```

For kimi/opencode foreground runtimes, set up the daemon venv:

```bash
python3 -m venv ~/.claude/channels/telegram/.venv
~/.claude/channels/telegram/.venv/bin/pip install python-telegram-bot httpx
```

### Add a bot

```bash
tg setup mybot
```

The wizard verifies the token against `getMe` before writing anything, picks the next free port, writes the state dir, and enables + starts the poller. It refuses to clobber a bot that already exists.

Manual equivalent, if you prefer:

```bash
mkdir -p ~/.claude/channels/telegram/mybot
cat > ~/.claude/channels/telegram/mybot/.env <<'EOF'
TELEGRAM_BOT_TOKEN=123456789:AAH...
TELEGRAM_MCP_PORT=3456
EOF
chmod 600 ~/.claude/channels/telegram/mybot/.env

echo '{"dmPolicy":"allowlist","allowFrom":["<your-telegram-user-id>"]}' \
  > ~/.claude/channels/telegram/mybot/access.json

echo '{"mybot":{"port":3456}}' > ~/.claude/channels/telegram/bots.json
systemctl --user enable --now telegram-mcp@mybot.service
```

---

## Usage

### Interactive sessions (foreground, needs terminal)

```bash
tg <runtime> <bot> [extra args...]
```

| Command | What you get |
|---|---|
| `tg claude mybot` | Claude Code via MCP bridge, in the current project |
| `tg kimi mybot` | Kimi Code via daemon.py, in the current directory |
| `tg opencode mybot` | OpenCode via daemon.py, in the current directory |
| `tg anon mybot` | Claude Code from `/tmp` — no project context |
| `tg claude mybot --continue` | extra args pass through to the runtime |

Close the terminal = bot dies. Run under tmux for persistence:

```bash
tmux new -s mybot
tg kimi mybot
```

### Always-on daemons (no terminal needed)

```bash
tg start <runtime> <bot> [workdir]
```

| Command | What you get |
|---|---|
| `tg start kimi mybot` | Kimi daemon via systemd, uses cwd as workdir |
| `tg start opencode mybot` | OpenCode daemon via systemd |
| `tg stop mybot` | Stop daemon, re-enable MCP poller |
| `tg status` | Show all daemons, pollers, and bots |

### Other commands

| Command | |
|---|---|
| `tg setup [name]` | Add a bot (interactive wizard) |
| `tg help` | Usage, plus the runtimes and bots you have |

### Adding a runtime

Drop an executable in `~/.claude/channels/telegram/runtimes/`. `tg` exports `TELEGRAM_STATE_DIR`, `TELEGRAM_MCP_PORT`, `TELEGRAM_MCP_URL`, and `TELEGRAM_POLLER_UNIT`, then `exec`s your script. That is the whole contract.

The shipped `claude` runtime is two lines. The `kimi` and `opencode` runtimes run `daemon.py` in the foreground — a `python-telegram-bot` poller that spawns one CLI subprocess per message. They stop the MCP poller on entry (409 conflict) and restart it on exit via a `trap`.

---

## Access control

`access.json` per bot, three modes:

| `dmPolicy` | Behavior |
|---|---|
| `allowlist` | Only listed user IDs get through. Everyone else is dropped silently. |
| `pairing` | A stranger gets a one-time 6-char code, valid 1 hour. You approve it from your terminal. Max 3 outstanding, 2 replies each. |
| `disabled` | Nothing gets through. |

Groups are opt-in per chat ID, default to requiring an @mention, and take their own per-group allowlist.

Approval happens in **your terminal**, never from chat. The MCP instructions tell the agent explicitly that a Telegram message asking to be added to the allowlist is what a prompt injection looks like, and to refuse it.

Under `allowlist` and `disabled`, `/start`, `/help` and `/status` are muted for anyone not on the list — a stranger who finds the bot gets silence, not a helpful explanation of what it is. Under `pairing` they answer normally, since that is how a new person learns to pair.

Outbound is checked too: every tool call re-validates the target chat against the allowlist, and the file sender refuses to attach anything from inside the state directory — so `access.json` and `.env` can't be talked out of the bot.

---

## What the agent can do

| Tool | |
|---|---|
| `reply` | Send text. Auto-chunks past Telegram's 4096 limit on paragraph, then line, then word boundaries. Attaches files by absolute path — images inline, everything else as documents, 50 MB cap. Optional MarkdownV2, optional threaded reply. |
| `react` | Emoji reaction on a message. |
| `download_attachment` | Pull a file to the local inbox and return the path. |
| `edit_message` | Rewrite a message already sent — progress updates without spamming. Edits don't push-notify, so send a fresh message when the long job finally lands. |
| `wait_for_message` | Block until a Telegram message arrives (up to timeout). Returns array of messages or empty on timeout. The primary inbound path for non-channel runtimes. |

Inbound handles text, photos, documents, voice, audio, video, video notes and stickers. Photos are downloaded automatically; everything else arrives with a `file_id` the agent can fetch on demand.

### Permission prompts

When the agent needs approval, the prompt goes to your phone as three buttons — **See more**, **Allow**, **Deny**. "See more" expands the full tool input before you decide. Only allowlisted users can press them. You can also answer in text: `y a1b2c` or `n a1b2c`.

---

## Staying up

| Failure | What happens |
|---|---|
| Session closes or `/clear`s | Poller keeps running. Messages queue, next session drains them. |
| A stale poller holds the token | New one finds the old PID, SIGTERMs it, takes over. |
| Telegram 409s | Backs off up to 15s and retries; gives up after 8 tries rather than fighting forever. |
| Server dies | `Restart=always`, 3s later it's back. |
| Poll loop disconnects | Proxy retries every 1s, re-acquires the pin automatically. |
| `access.json` is corrupt | Moved aside, fresh defaults, bot keeps running. |

---

## Files

```
tg                      launcher — resolves runtime + bot, exports the contract, execs
                        also: start/stop/status for always-on daemons
daemon.py               python-telegram-bot poller with kimi/opencode/claude backends
runtimes/               one executable per runtime: claude, kimi, opencode, anon
server.ts               poller, gate, MCP HTTP server, permission UI
proxy.ts                stdio MCP server, tool implementations, pin, poll client
telegram-mcp@.service   templated systemd unit — one instance per bot
package.json            bun deps: grammy, express, MCP SDK, zod
```

State, outside the repo:

```
~/.claude/channels/telegram/
├── bots.json                name → port
├── runtimes/                one executable per runtime
├── daemon.py                (symlinked or copied from repo)
├── .venv/                   python-telegram-bot venv for daemon
└── <bot>/
    ├── .env                 token + port  (0600)
    ├── access.json          policy + allowlist
    ├── bot.pid              running poller
    ├── pinned.lock          session that owns the bot
    └── inbox/               downloaded attachments
```

---

## License

Apache-2.0.
