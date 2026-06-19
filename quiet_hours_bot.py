#!/usr/bin/env python3
"""Telegram group quiet-hours bot using only the Python standard library."""

from __future__ import annotations

import fcntl
import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


APP_DIR = Path(os.environ.get("QUIET_BOT_DATA_DIR", Path.home() / ".local/share/telegram-quiet-hours"))
STATE_FILE = APP_DIR / "state.json"
LOCK_FILE = APP_DIR / "run.lock"
TIMEZONE = ZoneInfo(os.environ.get("QUIET_BOT_TIMEZONE", "Asia/Yangon"))
LOCK_HOUR = int(os.environ.get("QUIET_BOT_LOCK_HOUR", "23"))
UNLOCK_HOUR = int(os.environ.get("QUIET_BOT_UNLOCK_HOUR", "9"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

SEND_PERMISSION_FIELDS = (
    "can_send_messages",
    "can_send_audios",
    "can_send_documents",
    "can_send_photos",
    "can_send_videos",
    "can_send_video_notes",
    "can_send_voice_notes",
    "can_send_polls",
    "can_send_other_messages",
    "can_add_web_page_previews",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("telegram-quiet-hours")


class TelegramError(RuntimeError):
    pass


def api(method: str, **params: Any) -> Any:
    if not TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")
    body = json.dumps(params).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TelegramError(f"Telegram request failed: {exc}") from exc
    if not payload.get("ok"):
        raise TelegramError(f"{method}: {payload.get('description', 'unknown error')}")
    return payload.get("result")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"update_offset": 0, "chats": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {STATE_FILE}: {exc}") from exc


def save_state(state: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.chmod(temp, 0o600)
    temp.replace(STATE_FILE)


def desired_mode(now: datetime) -> str:
    hour = now.hour
    return "locked" if hour >= LOCK_HOUR or hour < UNLOCK_HOUR else "unlocked"


def is_admin(chat_id: int, user_id: int) -> bool:
    member = api("getChatMember", chat_id=chat_id, user_id=user_id)
    return member.get("status") in {"creator", "administrator"}


def setup_chat(message: dict[str, Any], state: dict[str, Any]) -> None:
    chat = message.get("chat", {})
    sender = message.get("from", {})
    chat_id = chat.get("id")
    if chat.get("type") not in {"group", "supergroup"} or not chat_id:
        api("sendMessage", chat_id=chat_id, text="Please use /setup inside the linked discussion group.")
        return
    if not is_admin(chat_id, sender.get("id", 0)):
        api("sendMessage", chat_id=chat_id, text="Only a group administrator can run /setup.")
        return

    try:
        configure_chat(chat_id, state, chat.get("title"))
    except TelegramError as exc:
        api(
            "sendMessage",
            chat_id=chat_id,
            text=str(exc),
        )
        return
    api(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"Setup complete.\nGroup ID: {chat_id}\n"
            "Quiet hours: 11:00 PM–9:00 AM (Asia/Yangon).\n"
            "Non-admin members cannot send messages during quiet hours."
        ),
    )


def configure_chat(chat_id: int, state: dict[str, Any], title: str | None = None) -> None:
    me = api("getMe")
    bot_member = api("getChatMember", chat_id=chat_id, user_id=me["id"])
    if bot_member.get("status") != "administrator" or not bot_member.get("can_restrict_members"):
        raise TelegramError(
            "Make me an administrator with Restrict Members permission, then configure the group again."
        )
    details = api("getChat", chat_id=chat_id)
    if details.get("type") not in {"group", "supergroup"}:
        raise TelegramError("The chat ID must belong to a group or supergroup.")
    existing = state.get("chats", {}).get(str(chat_id), {})
    permissions = existing.get("day_permissions") or details.get("permissions", {})
    state.setdefault("chats", {})[str(chat_id)] = {
        "title": title or details.get("title", "Telegram group"),
        "day_permissions": permissions,
        "mode": existing.get("mode"),
    }
    entry = state["chats"][str(chat_id)]
    save_state(state)
    apply_mode(chat_id, entry, desired_mode(datetime.now(TIMEZONE)))
    save_state(state)


def process_updates(state: dict[str, Any]) -> None:
    updates = api("getUpdates", offset=state.get("update_offset", 0), timeout=0, limit=100)
    for update in updates:
        state["update_offset"] = update["update_id"] + 1
        message = update.get("message") or update.get("edited_message")
        if not message:
            continue
        words = (message.get("text") or "").split(maxsplit=1)
        text = words[0].split("@", 1)[0].lower() if words else ""
        if text == "/setup":
            setup_chat(message, state)
        elif text == "/status":
            chat_id = message.get("chat", {}).get("id")
            entry = state.get("chats", {}).get(str(chat_id))
            if entry:
                api(
                    "sendMessage",
                    chat_id=chat_id,
                    text=f"Group ID: {chat_id}\nCurrent mode: {entry.get('mode') or 'pending'}",
                )
    save_state(state)


def apply_mode(chat_id: int, entry: dict[str, Any], mode: str) -> None:
    if entry.get("mode") == mode:
        return
    day_permissions = entry.get("day_permissions", {})
    if mode == "locked":
        permissions = dict(day_permissions)
        for field in SEND_PERMISSION_FIELDS:
            permissions[field] = False
    else:
        permissions = day_permissions

    api(
        "setChatPermissions",
        chat_id=chat_id,
        permissions=permissions,
        use_independent_chat_permissions=True,
    )
    entry["mode"] = mode
    entry["updated_at"] = datetime.now(TIMEZONE).isoformat()
    LOG.info("Applied %s mode to chat %s (%s)", mode, chat_id, entry.get("title", ""))


def tick() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info("Another run is active; skipping")
            return
        state = load_state()
        process_updates(state)
        mode = desired_mode(datetime.now(TIMEZONE))
        for chat_id, entry in state.get("chats", {}).items():
            try:
                apply_mode(int(chat_id), entry, mode)
            except TelegramError:
                LOG.exception("Could not update chat %s", chat_id)
        save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Group Night Guard")
    subcommands = parser.add_subparsers(dest="command")
    configure = subcommands.add_parser("configure", help="Configure a group by numeric chat ID")
    configure.add_argument("chat_id", type=int)
    args = parser.parse_args()
    if args.command == "configure":
        state = load_state()
        configure_chat(args.chat_id, state)
        entry = state["chats"][str(args.chat_id)]
        print(f"Configured {entry['title']} ({args.chat_id}); mode={entry['mode']}")
    else:
        tick()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("Bot run failed")
        sys.exit(1)
