#!/usr/bin/env python3
"""kimittyd-bridge: type Telegram DMs into the kimittyd tmux session and
send Kimi's text replies back to Telegram.

Usage: kimittyd-bridge.py <bot_name> <tmux_session_name> <work_dir>
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from telegram import Update
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, MessageHandler, filters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
for noisy in ("httpx", "httpcore", "telegram.request", "telegram.ext"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("kimittyd-bridge")

BOT_NAME = sys.argv[1]
SESSION = sys.argv[2]
WORK_DIR = Path(sys.argv[3]).resolve()
KIMI_HOME = Path(os.environ.get("KIMI_CODE_HOME", Path.home() / ".kimi-code"))
SESSION_INDEX = KIMI_HOME / "session_index.jsonl"

BASE_DIR = Path.home() / ".claude" / "channels" / "telegram"
BOT_DIR = BASE_DIR / BOT_NAME

_env = {}
with open(BOT_DIR / ".env") as f:
    for line in f:
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            _env[k] = v
TOKEN = _env["TELEGRAM_BOT_TOKEN"]

_access = json.loads((BOT_DIR / "access.json").read_text())
ALLOWED = {str(uid) for uid in _access.get("allowFrom", [])}

_locks: dict[str, asyncio.Lock] = {}
MAX_TG_LEN = 4096


async def _wait_for_session(timeout: float = 30.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", SESSION,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if proc.returncode == 0:
            return True
        await asyncio.sleep(0.5)
    log.error("tmux session %s never appeared", SESSION)
    return False


async def _send_keys(text: str) -> bool:
    """Send text to the TUI using bracketed-paste semantics.

    Wrapping the payload in CSI 200~/201~ tells the terminal application that
    this is a paste block, not typed keystrokes. This preserves newlines and
    special characters and matches how real terminals handle pasted text.
    Reference: https://cirw.in/blog/bracketed-paste
    """
    paste = f"\x1b[200~{text}\x1b[201~"
    proc = await asyncio.create_subprocess_exec(
        "tmux", "send-keys", "-t", SESSION, "-l", paste,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("send-keys paste block failed: %s", stderr.decode().strip())
        return False

    proc = await asyncio.create_subprocess_exec(
        "tmux", "send-keys", "-t", SESSION, "Enter",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("send-keys Enter failed: %s", stderr.decode().strip())
        return False
    return True


def _find_latest_session(after_offset: int = 0) -> Path | None:
    """Find the newest Kimi session for this working directory.

    session_index.jsonl is append-only, so the last matching line is the
    active session.  When after_offset > 0, only lines appended after that
    byte offset are considered — this forces the caller to wait for a NEW
    session rather than locking onto a stale one.
    """
    if not SESSION_INDEX.exists():
        return None
    latest = None
    try:
        with open(SESSION_INDEX, "r", encoding="utf-8", errors="replace") as f:
            if after_offset:
                f.seek(after_offset)
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("workDir") == str(WORK_DIR):
                    latest = Path(entry["sessionDir"])
    except Exception as e:
        log.error("failed to read session index: %s", e)
    return latest


def _extract_assistant_text(line: str) -> str | None:
    """Extract assistant text from a wire.jsonl line.

    We listen for the final `agent.message.appended` event, which contains the
    full assistant message with all content parts.
    """
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        return None

    if evt.get("type") != "agent.message.appended":
        return None

    msg = evt.get("message", {}).get("message", {})
    if msg.get("role") != "assistant":
        return None

    texts = []
    for part in msg.get("content", []):
        if isinstance(part, dict) and part.get("type") == "text":
            texts.append(part.get("text", ""))
    text = "".join(texts).strip()
    return text or None


async def _poll_wire(wire_file: Path, wire_offset: int,
                     deadline: float) -> str | None:
    """Poll a wire.jsonl from wire_offset until an assistant reply appears."""
    offset = wire_offset
    while asyncio.get_event_loop().time() < deadline:
        try:
            size = wire_file.stat().st_size
        except OSError:
            await asyncio.sleep(0.3)
            continue
        if size > offset:
            try:
                with open(wire_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    for line in f:
                        text = _extract_assistant_text(line)
                        if text:
                            return text
            except Exception as e:
                log.error("error reading wire.jsonl: %s", e)
            offset = size
        await asyncio.sleep(0.3)
    return None


async def _wait_for_reply(update: Update, timeout: float = 120.0,
                          index_offset: int = 0,
                          current_wire: Path | None = None,
                          wire_offset: int = 0) -> str | None:
    """Monitor wire.jsonl for assistant reply. Handles both cases:
    1. Kimi reuses existing session — new data in current_wire after wire_offset
    2. Kimi creates new session — new entry in session_index.jsonl after index_offset
    """
    deadline = asyncio.get_event_loop().time() + timeout

    # If we already have a live session, watch its wire.jsonl for growth
    # while simultaneously checking for a new session.
    while asyncio.get_event_loop().time() < deadline:
        # Check for new session
        new_session = _find_latest_session(after_offset=index_offset)
        if new_session:
            new_wire = new_session / "agents" / "main" / "wire.jsonl"
            if new_wire.exists() and new_wire != current_wire:
                log.info("new session detected: %s", new_session.name)
                # New session — watch its wire.jsonl from start
                result = await _poll_wire(new_wire, 0, deadline)
                if result:
                    return result

        # Check existing session wire.jsonl for growth
        if current_wire and current_wire.exists():
            try:
                size = current_wire.stat().st_size
            except OSError:
                size = 0
            if size > wire_offset:
                try:
                    with open(current_wire, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(wire_offset)
                        for line in f:
                            text = _extract_assistant_text(line)
                            if text:
                                return text
                except Exception as e:
                    log.error("error reading wire.jsonl: %s", e)
                wire_offset = size

        await asyncio.sleep(0.3)

    return None


async def _send_reply(update: Update, text: str) -> None:
    """Send text back to Telegram, chunking if needed."""
    for i in range(0, len(text), MAX_TG_LEN):
        await update.message.reply_text(text[i : i + MAX_TG_LEN])


async def handle_message(update: Update, context) -> None:
    if not update.effective_user or not update.message or update.message.text is None:
        return
    user_id = str(update.effective_user.id)
    if user_id not in ALLOWED:
        return

    text = update.message.text
    log.info("<- %s: %s", user_id, text[:200].replace("\n", " "))

    lock = _locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        # Snapshot BEFORE sending keys: both session index and current wire.jsonl
        index_offset = SESSION_INDEX.stat().st_size if SESSION_INDEX.exists() else 0
        current_session = _find_latest_session()
        current_wire = None
        wire_offset = 0
        if current_session:
            cw = current_session / "agents" / "main" / "wire.jsonl"
            if cw.exists():
                current_wire = cw
                wire_offset = cw.stat().st_size

        await update.message.chat.send_action("typing")
        ok = await _send_keys(text)
        if not ok:
            await update.message.reply_text("(failed to forward to TUI)")
            return

        reply = await _wait_for_reply(
            update, index_offset=index_offset,
            current_wire=current_wire, wire_offset=wire_offset,
        )
        if reply:
            log.info("-> %s: %d chars", user_id, len(reply))
            await _send_reply(update, reply)
        else:
            await update.message.reply_text("(no reply captured from TUI)")


async def handle_start(update: Update, context) -> None:
    if not update.effective_user:
        return
    user_id = str(update.effective_user.id)
    if user_id not in ALLOWED:
        return
    await update.message.reply_text(
        f"kimittyd on {BOT_NAME} is live at http://localhost:7682\n"
        "Type here or in the browser — both drive the same TUI."
    )


async def handle_reset(update: Update, context) -> None:
    if not update.effective_user:
        return
    user_id = str(update.effective_user.id)
    if user_id not in ALLOWED:
        return

    proc = await asyncio.create_subprocess_exec(
        "tmux", "send-keys", "-t", SESSION, "C-c",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    await update.message.reply_text("Sent Ctrl+C to the TUI.")


async def handle_error(update: object, context) -> None:
    """Log conflicts quietly; they mean another poller is on the same token."""
    if isinstance(context.error, Conflict):
        log.warning("Telegram conflict — another poller is using this token; exiting")
        raise context.error
    log.exception("Unhandled bridge error")


def main() -> None:
    if not asyncio.run(_wait_for_session()):
        sys.exit(1)

    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(handle_error)

    log.info("kimittyd-bridge: %s -> tmux session %s (cwd %s)", BOT_NAME, SESSION, WORK_DIR)
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
