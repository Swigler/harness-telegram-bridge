# Claude Code ↔ Telegram Bridge

A session-pinned Telegram bridge for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). The bot lives exactly as long as your terminal session — start it, use it, close it. No always-on daemon.

This is a fork of the official Claude Code Telegram channel plugin with a security patch and a portable deployment setup using tmux + Tailscale.

---

## How It Works

```
Phone (Telegram)
  │
  ▼
┌─────────────────────┐
│  server.ts           │  Standalone MCP HTTP server
│  Polls Telegram      │  Runs as a systemd user unit
│  Queues messages     │  Starts/stops with the pin
└──────────┬──────────┘
           │ SSE (/events)
           ▼
┌─────────────────────┐
│  proxy.ts            │  Stdio MCP proxy
│  Bridges to Claude   │  Spawned by Claude Code
│  Owns the pin lock   │  One session at a time
└──────────┬──────────┘
           │ stdio
           ▼
┌─────────────────────┐
│  Claude Code         │  Your session
│  Reads messages      │  Calls reply/react/edit
│  Full tool access    │  Permission buttons in TG
└─────────────────────┘
```

**The pin design:** Only one Claude session can own the bot at a time. `tgpin` acquires a lock file, starts the poller, and releases both when the session ends. This prevents the 409 Conflict that happens when two pollers fight over the same Telegram token.

---

## Security Patch

The upstream plugin has a disclosure issue: `/start`, `/help`, and `/status` commands are registered before the access gate runs. Under `dmPolicy: "allowlist"`, a stranger who finds the bot gets a helpful response explaining it's a Claude Code bridge — leaking that the bot exists and what it does.

**The patch** adds a `commandMuted()` guard: under allowlist or disabled mode, commands from non-allowlisted users are silently dropped. Under pairing mode, they work normally (since `/start` is how new users learn to pair).

This is +15 lines, no deletions, visible in the git diff.

---

## Setup

### Prerequisites
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed
- [Bun](https://bun.sh) runtime
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### 1. Install the server

```bash
mkdir -p ~/.claude/telegram-server
cp server.ts proxy.ts package.json ~/.claude/telegram-server/
cd ~/.claude/telegram-server && bun install
```

### 2. Configure the bot token

```bash
mkdir -p ~/.claude/channels/telegram
echo "TELEGRAM_BOT_TOKEN=YOUR_TOKEN_HERE" > ~/.claude/channels/telegram/.env
chmod 600 ~/.claude/channels/telegram/.env
```

### 3. Install the systemd user unit

```bash
mkdir -p ~/.config/systemd/user
cp telegram-mcp.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

**Do not enable the service** — `tgpin` starts and stops it automatically. Enabling it would make the bot immortal and fight with the pin design.

### 4. Install the launcher

```bash
cp tgpin ~/bin/tgpin
chmod +x ~/bin/tgpin

# Optional: alias in your .bashrc
echo 'alias tg="~/bin/tgpin"' >> ~/.bashrc
```

### 5. Lock access (recommended)

By default, the bot is in pairing mode — anyone who DMs it gets a pairing code. To lock it to your Telegram user ID:

```bash
cat > ~/.claude/channels/telegram/access.json << 'EOF'
{
  "dmPolicy": "allowlist",
  "allowFrom": ["YOUR_TELEGRAM_USER_ID"],
  "groups": {},
  "pending": {}
}
EOF
```

Find your user ID by sending a message to [@userinfobot](https://t.me/userinfobot) on Telegram.

---

## Usage

### Start a session
```bash
tg              # start Claude with Telegram bridge
tg --continue   # resume the last conversation
```

### Portable access (tmux + Tailscale + Termius)

The real power is running this over SSH from your phone. The stack:

- **[Tailscale](https://tailscale.com)** — mesh VPN. Your phone and machine see each other on a private network, no port forwarding, no public IP needed. Free for personal use.
- **[Termius](https://termius.com)** — SSH client for Android/iOS. Supports key auth, persistent sessions, and Tailscale addresses. Free tier is enough.
- **tmux** — terminal multiplexer. The session survives SSH disconnects.

```bash
# On your machine (once):
tmux new -s claude
tg

# Detach: Ctrl+B, then D

# From your phone (Termius → Tailscale IP):
ssh your-machine
tmux attach -t claude
```

The bot stays live as long as the tmux session exists. SSH drops don't kill it. Close the tmux session and the bot dies — by design.

**The workflow:** You're on the bus, open Termius on your phone, SSH into your machine over Tailscale, attach to the tmux session — Claude is live on Telegram. Close Termius, the tmux session persists, the bot keeps running. You pick it back up later from anywhere.

### Permission handling

Tool calls surface as approve/deny buttons in Telegram. The session runs in `--permission-mode default`, so destructive operations (file writes, shell commands) require your explicit tap before executing.

---

## Architecture Decisions

### Why session-pinned?
An always-on bot means an always-on Claude session consuming resources and potentially acting on stale context. The pin design means the bot is live when you want it, dead when you don't. This is a feature, not a limitation.

### Why two files (server.ts + proxy.ts)?
The server runs as a systemd unit and holds the Telegram polling connection. The proxy is spawned by Claude as a stdio MCP transport. Separating them means:
- The server can restart independently of Claude
- The proxy can reconnect to a running server
- No polling state is lost during a Claude session restart

### Why not a webhook?
Webhooks need a public URL, TLS, and port forwarding. Long polling works anywhere — behind NAT, on a laptop, on a VPS. Zero infrastructure beyond the machine itself.

### One poller per token
Telegram's Bot API returns 409 Conflict if two processes poll the same token. The lock file (`pinned.lock`) enforces exactly one poller. If a session crashes without cleanup, the next `tgpin` detects the stale PID and reclaims the lock.

---

## Files

| File | Purpose |
|---|---|
| `server.ts` | Standalone MCP HTTP server — polls Telegram, queues messages, serves tools |
| `proxy.ts` | Stdio MCP proxy — bridges server ↔ Claude, manages pin lifecycle |
| `package.json` | Dependencies: grammy, MCP SDK, express, zod |
| `tgpin` | Launcher script — acquires pin, starts Claude with the channel loaded |
| `telegram-mcp.service` | systemd user unit for the server |

---

## License

Apache-2.0 (same as the upstream Claude Code Telegram plugin).

---

## Contact

- GitHub: [Swigler](https://github.com/Swigler)
