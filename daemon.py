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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, CommandHandler, filters

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
if len(sys.argv) >= 3 and sys.argv[1] in ("claude", "kimi", "opencode", "openclaude"):
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

# Inbound voice/audio transcription (faster-whisper, uv venv — see .claude/rules/telegram.md)
TRANSCRIBE_CMD = [
    str(Path.home() / "stt-tts" / ".venv" / "bin" / "python"),
    str(Path.home() / "stt-tts" / "transcribe.py"),
]

# Outbound TTS (edge-tts via uvx) — every reply gets audio, done in code so no
# agent can forget it. Daemon equivalent of Claude's PostToolUse TTS hook.
RO_CHARS = set("ăâîșțĂÂÎȘȚ")


def _sid_file(user_id: str) -> Path:
    """Per-user file holding the last kimi session ID (survives daemon restarts)."""
    return BOT_DIR / f"kimi_session_{user_id}.id"

# ── Approval relay ───────────────────────────────────────────────────────

_pending_approvals: dict[str, asyncio.Future] = {}  # req_id -> Future[str]


async def _relay_approval(req, update: Update) -> None:
    """Send approval request to Telegram as inline keyboard, wait for response."""
    desc = f"🔐 *Permission request*\n`{req.action}`\n{req.description}"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"a:{req.id}"),
            InlineKeyboardButton("✅ Session", callback_data=f"s:{req.id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"r:{req.id}"),
        ]
    ])
    await update.message.reply_text(desc, reply_markup=keyboard, parse_mode="Markdown")

    future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
    _pending_approvals[req.id] = future
    try:
        return await future
    finally:
        _pending_approvals.pop(req.id, None)


async def handle_approval_callback(update: Update, context) -> None:
    """Handle inline keyboard button press for approval requests."""
    query = update.callback_query
    await query.answer()
    data = query.data
    if ":" not in data:
        return
    action, req_id = data.split(":", 1)
    future = _pending_approvals.get(req_id)
    if not future or future.done():
        await query.edit_message_text("(expired)")
        return

    response_map = {"a": "approve", "s": "approve_for_session", "r": "reject"}
    response = response_map.get(action, "reject")
    future.set_result(response)

    label = {"a": "✅ Approved", "s": "✅ Approved for session", "r": "❌ Rejected"}
    await query.edit_message_text(f"{label.get(action, '❌ Rejected')}")


# ── Backends ──────────────────────────────────────────────────────────────

sessions = {}  # user_id -> session handle


async def _respond_claude(user_id: str, text: str, update: Update) -> str:
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


async def _respond_kimi(user_id: str, text: str, update: Update) -> str:
    kimi_bin = Path.home() / ".kimi-code" / "bin" / "kimi"
    proc = await asyncio.create_subprocess_exec(
        str(kimi_bin), "-p", text, "--output-format", "text",
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # flows to terminal for live visibility
        cwd=str(WORK_DIR),
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip() or "(no response)"


async def _respond_opencode(user_id: str, text: str, update: Update) -> str:
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


async def _respond_openclaude(user_id: str, text: str, update: Update) -> str:
    """OpenClaude via LiteLLM (DeepSeek free + paid fallback)."""
    oc_bin = Path.home() / ".local" / "bin" / "openclaude"
    env = {
        **os.environ,
        "CLAUDE_CODE_USE_OPENAI": "1",
        "OPENAI_BASE_URL": "http://localhost:8080/v1",
        "OPENAI_API_KEY": os.environ.get("LITELLM_KEY", "sk-1234"),
        "OPENAI_MODEL": "fast",
    }
    proc = await asyncio.create_subprocess_exec(
        str(oc_bin), "-p", "--output-format", "json",
        "--dangerously-skip-permissions", text,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,
        cwd=str(WORK_DIR),
        env=env,
    )
    stdout, _ = await proc.communicate()
    chunks = []
    for line in stdout.decode().splitlines():
        try:
            evt = json.loads(line)
            if evt.get("type") == "assistant":
                for block in evt.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        chunks.append(block["text"])
            elif evt.get("type") == "result":
                if evt.get("result"):
                    chunks.append(evt["result"])
        except (json.JSONDecodeError, KeyError):
            pass
    return "".join(chunks) or "(no response)"


BACKENDS = {
    "claude": _respond_claude,
    "kimi": _respond_kimi,
    "opencode": _respond_opencode,
    "openclaude": _respond_openclaude,
}

respond = BACKENDS[RUNTIME]

# ── Telegram handlers ─────────────────────────────────────────────────────


async def _send_tts(update: Update, text: str):
    """Outbound voice: edge-tts the reply, send as audio. Runs for ALL runtimes.

    Daemon-native equivalent of the MCP bridge's TTS rule — done in code so no
    agent can forget it (Radu requirement, ALL runtimes, reconfirmed 2026-09-21).
    Failure is logged, never raised: the text reply is the primary channel.
    """
    voice = "ro-RO-AlinaNeural" if any(c in RO_CHARS for c in text) else "en-GB-SoniaNeural"
    out = Path("/tmp") / f"tts_{BOT_NAME}_{os.getpid()}_{os.urandom(4).hex()}.mp3"
    try:
        proc = await asyncio.create_subprocess_exec(
            "uvx", "edge-tts", "--voice", voice, "--text", text,
            "--write-media", str(out),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not out.exists():
            log.error("edge-tts failed (rc=%s): %s", proc.returncode, stderr.decode()[-500:])
            return
        with open(out, "rb") as f:
            await update.message.reply_audio(audio=f, caption="🔊")
    except Exception:
        log.exception("TTS send failed")
    finally:
        out.unlink(missing_ok=True)


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
        response = await respond(user_id, text, update)
        log.info("→ %s: %d chars", user_id, len(response))

        # Split long messages
        for i in range(0, len(response), MAX_TG_LEN):
            await update.message.reply_text(response[i : i + MAX_TG_LEN])
        await _send_tts(update, response)
    except Exception as e:
        log.exception("Error handling message")
        await update.message.reply_text(f"Error: {type(e).__name__}: {e}")


async def handle_voice(update: Update, context):
    """Inbound voice/audio: download, transcribe (faster-whisper), respond to the text.

    This is the daemon-native equivalent of the MCP bridge's STT rule — done in
    code so no agent can forget it. Runs for ALL runtimes (kimi/opencode/claude).
    """
    if not update.effective_user or not update.message:
        return
    user_id = str(update.effective_user.id)
    if user_id not in ALLOWED:
        return
    media = update.message.voice or update.message.audio
    if not media:
        return

    log.info("← %s: voice/audio (%s bytes)", user_id, media.file_size)
    try:
        await update.message.chat.send_action("typing")
        tg_file = await context.bot.get_file(media.file_id)
        local = Path("/tmp") / f"tg_voice_{media.file_unique_id}.ogg"
        await tg_file.download_to_drive(local)

        proc = await asyncio.create_subprocess_exec(
            *TRANSCRIBE_CMD, str(local),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        local.unlink(missing_ok=True)

        text = stdout.decode().strip()
        if proc.returncode != 0 or not text:
            log.error("transcribe failed (rc=%s): %s", proc.returncode, stderr.decode()[-500:])
            await update.message.reply_text("(voice transcription failed)")
            return

        log.info("← %s transcribed: %s", user_id, text[:200])
        response = await respond(user_id, text, update)
        log.info("→ %s: %d chars", user_id, len(response))
        for i in range(0, len(response), MAX_TG_LEN):
            await update.message.reply_text(response[i : i + MAX_TG_LEN])
        await _send_tts(update, response)
    except Exception as e:
        log.exception("Error handling voice")
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
    app.add_handler(CallbackQueryHandler(handle_approval_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler((filters.VOICE | filters.AUDIO) & ~filters.COMMAND, handle_voice))

    log.info("tg-daemon: %s on %s (cwd %s)", RUNTIME, BOT_NAME, WORK_DIR)
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
