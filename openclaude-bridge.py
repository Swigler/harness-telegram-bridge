#!/usr/bin/env python3
"""openclaude-bridge: type Telegram DMs into an openclaude tmux session and
send replies back to Telegram.

Usage: openclaude-bridge.py <bot_name> <tmux_session_name> <work_dir>

OpenClaude stores sessions in ~/.openclaude/projects/<path-hash>/<session-id>.jsonl.
We watch history.jsonl for new session IDs, then tail the session JSONL for
assistant messages — same dual-watch pattern as kimittyd-bridge.
"""

import asyncio
import json
import logging
import os
import re
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

log = logging.getLogger("openclaude-bridge")

BOT_NAME = sys.argv[1]
SESSION = sys.argv[2]
WORK_DIR = Path(sys.argv[3]).resolve()
OC_HOME = Path(os.environ.get("OPENCLAUDE_HOME", Path.home() / ".openclaude"))
HISTORY = OC_HOME / "history.jsonl"

# OpenClaude encodes project path as dash-separated: /home/user/foo -> -home-user-foo
PROJECT_HASH = str(WORK_DIR).replace("/", "-")
PROJECTS_DIR = OC_HOME / "projects" / PROJECT_HASH

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

# Outbound TTS
RO_CHARS = set("ăâîșțĂÂÎȘȚ")


async def _send_tts(update: Update, text: str):
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


def _find_latest_session_file(after_offset: int = 0) -> Path | None:
    """Find newest openclaude session JSONL for this work dir.

    history.jsonl is append-only. Each line has {project, sessionId}.
    Session file: ~/.openclaude/projects/<path-hash>/<sessionId>.jsonl
    """
    if not HISTORY.exists():
        return None
    latest = None
    try:
        with open(HISTORY, "r", encoding="utf-8", errors="replace") as f:
            if after_offset:
                f.seek(after_offset)
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("project") == str(WORK_DIR):
                    sid = entry.get("sessionId")
                    if sid:
                        candidate = PROJECTS_DIR / f"{sid}.jsonl"
                        latest = candidate
    except Exception as e:
        log.error("failed to read history.jsonl: %s", e)
    return latest


def _extract_assistant_text(line: str) -> str | None:
    """Extract assistant text from an openclaude session JSONL line."""
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        return None

    if evt.get("type") != "assistant":
        return None

    msg = evt.get("message", {})
    if msg.get("role") != "assistant":
        return None

    texts = []
    for part in msg.get("content", []):
        if isinstance(part, dict) and part.get("type") == "text":
            texts.append(part.get("text", ""))
    text = "".join(texts).strip()
    return text or None


async def _poll_session(session_file: Path, offset: int,
                        deadline: float) -> str | None:
    """Poll a session JSONL from offset until an assistant reply appears."""
    while asyncio.get_event_loop().time() < deadline:
        try:
            size = session_file.stat().st_size
        except OSError:
            await asyncio.sleep(0.3)
            continue
        if size > offset:
            last_text = None
            try:
                with open(session_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    for line in f:
                        text = _extract_assistant_text(line)
                        if text:
                            last_text = text
            except Exception as e:
                log.error("error reading session file: %s", e)
            if last_text:
                return last_text
            offset = size
        await asyncio.sleep(0.3)
    return None


async def _wait_for_reply(update: Update, timeout: float = 120.0,
                          history_offset: int = 0,
                          current_session: Path | None = None,
                          session_offset: int = 0) -> str | None:
    """Monitor session JSONL for assistant reply. Dual-watch:
    1. Current session file growth (reuse)
    2. New session in history.jsonl (new session created)
    """
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        # Check for new session
        new_session = _find_latest_session_file(after_offset=history_offset)
        if new_session and new_session.exists() and new_session != current_session:
            log.info("new session detected: %s", new_session.name)
            result = await _poll_session(new_session, 0, deadline)
            if result:
                return result

        # Check existing session for growth
        if current_session and current_session.exists():
            try:
                size = current_session.stat().st_size
            except OSError:
                size = 0
            if size > session_offset:
                last_text = None
                try:
                    with open(current_session, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(session_offset)
                        for line in f:
                            text = _extract_assistant_text(line)
                            if text:
                                last_text = text
                except Exception as e:
                    log.error("error reading session file: %s", e)
                if last_text:
                    return last_text
                session_offset = size

        await asyncio.sleep(0.3)

    return None


async def _send_reply(update: Update, text: str) -> None:
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
        history_offset = HISTORY.stat().st_size if HISTORY.exists() else 0
        current_session = _find_latest_session_file()
        session_offset = 0
        if current_session and current_session.exists():
            session_offset = current_session.stat().st_size

        await update.message.chat.send_action("typing")
        ok = await _send_keys(text)
        if not ok:
            await update.message.reply_text("(failed to forward to TUI)")
            return

        reply = await _wait_for_reply(
            update, history_offset=history_offset,
            current_session=current_session, session_offset=session_offset,
        )
        if reply:
            log.info("-> %s: %d chars", user_id, len(reply))
            await _send_reply(update, reply)
            await _send_tts(update, reply)
        else:
            await update.message.reply_text("(no reply captured from TUI)")


async def handle_start(update: Update, context) -> None:
    if not update.effective_user:
        return
    user_id = str(update.effective_user.id)
    if user_id not in ALLOWED:
        return
    await update.message.reply_text(
        f"openclaudeweb on {BOT_NAME} is live at http://localhost:7683\n"
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

    log.info("openclaude-bridge: %s -> tmux session %s (cwd %s)", BOT_NAME, SESSION, WORK_DIR)
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
