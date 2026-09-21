#!/usr/bin/env python3
"""tg-daemon: always-on Telegram bot with coding agent backends.

Usage: daemon.py <runtime> <bot_name> [work_dir]
  runtime:  claude | kimi | opencode
  bot_name: name from bots.json
  work_dir: project directory (default: $HOME)
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
# Pin httpx/httpcore/telegram to WARNING — token leak prevention
for noisy in ("httpx", "httpcore", "telegram.request", "telegram.ext"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("tg-daemon")
log.setLevel(logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────

# Accept either: daemon.py claude buzz [dir]  OR  daemon.py claude-buzz [dir]
if len(sys.argv) >= 3 and sys.argv[1] in ("claude", "kimi", "opencode"):
    RUNTIME = sys.argv[1]
    BOT_NAME = sys.argv[2]
    WORK_DIR = Path(sys.argv[3]) if len(sys.argv) > 3 else None
else:
    RUNTIME, BOT_NAME = sys.argv[1].split("-", 1)
    WORK_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else None

# Env override from systemd drop-in, then fallback to $HOME
WORK_DIR = WORK_DIR or Path(os.environ.get("TG_WORK_DIR", str(Path.home())))
BASE_DIR = Path.home() / ".claude" / "channels" / "telegram"
BOT_DIR = BASE_DIR / BOT_NAME

# Load token
_env = {}
with open(BOT_DIR / ".env") as f:
    for line in f:
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            _env[k] = v

TOKEN = _env["TELEGRAM_BOT_TOKEN"]

# Load allowlist
_access = json.loads((BOT_DIR / "access.json").read_text())
ALLOWED = {str(uid) for uid in _access.get("allowFrom", [])}

MAX_TG_LEN = 4096

# ── Backends ──────────────────────────────────────────────────────────────

sessions = {}  # user_id -> session handle


async def _respond_claude(user_id: str, text: str) -> str:
    from claude_agent_sdk import (
        ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock
    )

    if user_id not in sessions:
        opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(WORK_DIR),
        )
        client = ClaudeSDKClient(options=opts)
        await client.connect()
        sessions[user_id] = client

    client = sessions[user_id]
    await client.query(text, session_id=user_id)

    chunks = []
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage) and msg.content:
            for block in msg.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
        elif isinstance(msg, ResultMessage) and msg.result:
            chunks.append(msg.result)
    return "".join(chunks) or "(no response)"


async def _respond_kimi(user_id: str, text: str) -> str:
    kimi_bin = Path.home() / ".kimi-code" / "bin" / "kimi"
    proc = await asyncio.create_subprocess_exec(
        str(kimi_bin), "--prompt", text, "--output-format", "text",
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # flows to journal for live visibility
        cwd=str(WORK_DIR),
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip() or "(no response)"


async def _respond_opencode(user_id: str, text: str) -> str:
    oc_bin = Path.home() / ".opencode" / "bin" / "opencode"
    proc = await asyncio.create_subprocess_exec(
        str(oc_bin), "run", "--auto", "--format", "json", text,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # flows to journal for live visibility
        cwd=str(WORK_DIR),
    )
    stdout, _ = await proc.communicate()
    chunks = []
    for line in stdout.decode().splitlines():
        try:
            evt = json.loads(line)
            if evt.get("type") == "text":
                chunks.append(evt["part"]["text"])
        except (json.JSONDecodeError, KeyError):
            pass
    return "".join(chunks) or "(no response)"


BACKENDS = {
    "claude": _respond_claude,
    "kimi": _respond_kimi,
    "opencode": _respond_opencode,
}

respond = BACKENDS[RUNTIME]

# ── Telegram handlers ─────────────────────────────────────────────────────


async def handle_message(update: Update, context):
    if not update.effective_user or not update.message or not update.message.text:
        return
    user_id = str(update.effective_user.id)
    if user_id not in ALLOWED:
        return

    text = update.message.text
    log.info("← %s: %s", user_id, text[:200])

    try:
        await update.message.chat.send_action("typing")
        response = await respond(user_id, text)
        log.info("→ %s: %d chars", user_id, len(response))

        # Split long messages
        for i in range(0, len(response), MAX_TG_LEN):
            await update.message.reply_text(response[i : i + MAX_TG_LEN])
    except Exception as e:
        log.exception("Error handling message")
        await update.message.reply_text(f"Error: {type(e).__name__}: {e}")


async def handle_start(update: Update, context):
    if not update.effective_user:
        return
    user_id = str(update.effective_user.id)
    if user_id not in ALLOWED:
        return
    await update.message.reply_text(f"tg-daemon: {RUNTIME} active on {BOT_NAME}")


async def handle_reset(update: Update, context):
    """Reset session for this user."""
    if not update.effective_user:
        return
    user_id = str(update.effective_user.id)
    if user_id not in ALLOWED:
        return

    old = sessions.pop(user_id, None)
    if old and RUNTIME == "claude":
        try:
            await old.disconnect()
        except Exception:
            pass
    elif old and RUNTIME == "kimi":
        try:
            await old.close()
        except Exception:
            pass

    await update.message.reply_text("Session reset.")


# ── Main ──────────────────────────────────────────────────────────────────


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("tg-daemon: %s on %s (cwd %s)", RUNTIME, BOT_NAME, WORK_DIR)
    app.run_polling(allowed_updates=["message"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
