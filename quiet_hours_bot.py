#!/usr/bin/env python3
"""Telegram quiet-hours and community-assisted moderation bot."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sqlite3
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
DATABASE_FILE = APP_DIR / "moderation.db"
TICK_LOCK_FILE = APP_DIR / "run.lock"
DAEMON_LOCK_FILE = APP_DIR / "daemon.lock"
TIMEZONE = ZoneInfo(os.environ.get("QUIET_BOT_TIMEZONE", "Asia/Yangon"))
LOCK_HOUR = int(os.environ.get("QUIET_BOT_LOCK_HOUR", "23"))
UNLOCK_HOUR = int(os.environ.get("QUIET_BOT_UNLOCK_HOUR", "9"))
REPORT_THRESHOLD = int(os.environ.get("QUIET_BOT_REPORT_THRESHOLD", "3"))
AUTO_MUTE_THRESHOLD = int(os.environ.get("QUIET_BOT_AUTO_MUTE_THRESHOLD", "5"))
REPORT_MAX_AGE_MINUTES = int(os.environ.get("QUIET_BOT_REPORT_MAX_AGE_MINUTES", "30"))
REPORT_RATE_LIMIT = int(os.environ.get("QUIET_BOT_REPORT_RATE_LIMIT", "5"))
TEMP_MUTE_HOURS = int(os.environ.get("QUIET_BOT_TEMP_MUTE_HOURS", "1"))
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
LOG = logging.getLogger("telegram-group-night-guard")


class TelegramError(RuntimeError):
    pass


def api(method: str, *, request_timeout: int = 20, **params: Any) -> Any:
    if not TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            description = json.load(exc).get("description", str(exc))
        except (json.JSONDecodeError, AttributeError):
            description = str(exc)
        raise TelegramError(f"{method}: {description}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TelegramError(f"{method}: request failed: {exc}") from exc
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


def database() -> sqlite3.Connection:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_FILE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS cases (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            target_user_id INTEGER NOT NULL,
            target_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            alert_message_id INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS reports (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            reporter_id INTEGER NOT NULL,
            reported_at INTEGER NOT NULL,
            PRIMARY KEY (chat_id, message_id, reporter_id),
            FOREIGN KEY (chat_id, message_id) REFERENCES cases(chat_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            actor_id INTEGER NOT NULL,
            target_user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            message_id INTEGER,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reports_reporter_time
            ON reports(reporter_id, reported_at);
        """
    )
    try:
        os.chmod(DATABASE_FILE, 0o600)
    except OSError:
        pass
    return connection


def desired_mode(now: datetime) -> str:
    return "locked" if now.hour >= LOCK_HOUR or now.hour < UNLOCK_HOUR else "unlocked"


def parse_command(message: dict[str, Any]) -> tuple[str, list[str]]:
    words = (message.get("text") or "").split()
    if not words or not words[0].startswith("/"):
        return "", []
    command = words[0].split("@", 1)[0].lower()
    return command, words[1:]


def display_name(user: dict[str, Any]) -> str:
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    return name or user.get("username") or str(user.get("id", "unknown"))


def is_admin(chat_id: int, user_id: int) -> bool:
    if not user_id:
        return False
    member = api("getChatMember", chat_id=chat_id, user_id=user_id)
    return member.get("status") in {"creator", "administrator"}


def require_admin(message: dict[str, Any]) -> bool:
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    if not chat_id or not is_admin(chat_id, user_id):
        if chat_id:
            api("sendMessage", chat_id=chat_id, text="Only a group administrator can use this command.")
        return False
    return True


def ensure_target_can_be_moderated(chat_id: int, target_user_id: int) -> None:
    me = api("getMe")
    if target_user_id == me["id"]:
        raise TelegramError("I cannot moderate myself.")
    target = api("getChatMember", chat_id=chat_id, user_id=target_user_id)
    if target.get("status") in {"creator", "administrator"}:
        raise TelegramError("Administrators cannot be reported or banned by this bot.")


def setup_chat(message: dict[str, Any], state: dict[str, Any]) -> None:
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if chat.get("type") not in {"group", "supergroup"} or not chat_id:
        if chat_id:
            api("sendMessage", chat_id=chat_id, text="Use /setup inside the linked discussion group.")
        return
    if not require_admin(message):
        return
    try:
        configure_chat(chat_id, state, chat.get("title"))
    except TelegramError as exc:
        api("sendMessage", chat_id=chat_id, text=str(exc))
        return
    api(
        "sendMessage",
        chat_id=chat_id,
        text=(
            f"Setup complete.\nGroup ID: {chat_id}\n"
            f"Quiet hours: {LOCK_HOUR}:00–{UNLOCK_HOUR}:00 ({TIMEZONE.key}).\n"
            f"Reports: admin review at {REPORT_THRESHOLD}; auto-mute at {AUTO_MUTE_THRESHOLD}."
        ),
    )


def configure_chat(chat_id: int, state: dict[str, Any], title: str | None = None) -> None:
    me = api("getMe")
    bot_member = api("getChatMember", chat_id=chat_id, user_id=me["id"])
    if bot_member.get("status") != "administrator" or not bot_member.get("can_restrict_members"):
        raise TelegramError("Make me an administrator with Restrict Members permission.")
    if not bot_member.get("can_delete_messages"):
        raise TelegramError("Enable the Delete Messages administrator permission, then configure again.")
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


def moderation_keyboard(message_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Ban & delete", "callback_data": f"mod:ban:{message_id}"},
                {"text": f"Mute {TEMP_MUTE_HOURS}h", "callback_data": f"mod:mute:{message_id}"},
            ],
            [{"text": "Dismiss", "callback_data": f"mod:dismiss:{message_id}"}],
        ]
    }


def record_report(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    message_id: int,
    target_user_id: int,
    target_name: str,
    reporter_id: int,
    now: int,
) -> tuple[bool, int, str]:
    existing = connection.execute(
        "SELECT status FROM cases WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
    ).fetchone()
    if existing and existing["status"] not in {"open", "auto_muted"}:
        return False, 0, existing["status"]
    recent_count = connection.execute(
        "SELECT COUNT(*) FROM reports WHERE reporter_id = ? AND reported_at >= ?",
        (reporter_id, now - 3600),
    ).fetchone()[0]
    if recent_count >= REPORT_RATE_LIMIT:
        return False, -1, "rate_limited"
    connection.execute(
        """
        INSERT OR IGNORE INTO cases
            (chat_id, message_id, target_user_id, target_name, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'open', ?, ?)
        """,
        (chat_id, message_id, target_user_id, target_name, now, now),
    )
    cursor = connection.execute(
        "INSERT OR IGNORE INTO reports(chat_id, message_id, reporter_id, reported_at) VALUES (?, ?, ?, ?)",
        (chat_id, message_id, reporter_id, now),
    )
    count = connection.execute(
        "SELECT COUNT(*) FROM reports WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
    ).fetchone()[0]
    connection.commit()
    return cursor.rowcount == 1, count, existing["status"] if existing else "open"


def get_case(connection: sqlite3.Connection, chat_id: int, message_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT c.*, COUNT(r.reporter_id) AS report_count
        FROM cases c LEFT JOIN reports r
          ON r.chat_id = c.chat_id AND r.message_id = c.message_id
        WHERE c.chat_id = ? AND c.message_id = ?
        GROUP BY c.chat_id, c.message_id
        """,
        (chat_id, message_id),
    ).fetchone()


def set_case_status(
    connection: sqlite3.Connection,
    chat_id: int,
    message_id: int,
    status: str,
    *,
    alert_message_id: int | None = None,
) -> None:
    if alert_message_id is None:
        connection.execute(
            "UPDATE cases SET status = ?, updated_at = ? WHERE chat_id = ? AND message_id = ?",
            (status, int(time.time()), chat_id, message_id),
        )
    else:
        connection.execute(
            """
            UPDATE cases SET status = ?, alert_message_id = ?, updated_at = ?
            WHERE chat_id = ? AND message_id = ?
            """,
            (status, alert_message_id, int(time.time()), chat_id, message_id),
        )
    connection.commit()


def audit(
    connection: sqlite3.Connection,
    chat_id: int,
    actor_id: int,
    target_user_id: int,
    action: str,
    message_id: int | None,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_log(chat_id, actor_id, target_user_id, action, message_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (chat_id, actor_id, target_user_id, action, message_id, int(time.time())),
    )
    connection.commit()


def mute_permissions() -> dict[str, bool]:
    return {field: False for field in SEND_PERMISSION_FIELDS}


def mute_user(chat_id: int, user_id: int) -> None:
    api(
        "restrictChatMember",
        chat_id=chat_id,
        user_id=user_id,
        permissions=mute_permissions(),
        use_independent_chat_permissions=True,
        until_date=int(time.time()) + TEMP_MUTE_HOURS * 3600,
    )


def send_moderation_alert(chat_id: int, case: sqlite3.Row) -> int:
    result = api(
        "sendMessage",
        chat_id=chat_id,
        reply_to_message_id=case["message_id"],
        allow_sending_without_reply=True,
        text=(
            "Moderator review required\n"
            f"Reported user: {case['target_name']}\n"
            f"Unique reports: {case['report_count']}\n"
            "Only administrators can use these buttons."
        ),
        reply_markup=moderation_keyboard(case["message_id"]),
    )
    return result["message_id"]


def handle_report(message: dict[str, Any]) -> None:
    chat = message.get("chat", {})
    reporter = message.get("from", {})
    replied = message.get("reply_to_message")
    chat_id = chat.get("id")
    if chat.get("type") not in {"group", "supergroup"} or not chat_id:
        return
    if not replied or not replied.get("from"):
        api("sendMessage", chat_id=chat_id, text="Reply to the offending message with /report.")
        return
    target = replied["from"]
    if target.get("id") == reporter.get("id"):
        api("sendMessage", chat_id=chat_id, text="You cannot report yourself.")
        return
    if int(time.time()) - int(replied.get("date", 0)) > REPORT_MAX_AGE_MINUTES * 60:
        api(
            "sendMessage",
            chat_id=chat_id,
            text=f"Only messages from the last {REPORT_MAX_AGE_MINUTES} minutes can be reported.",
        )
        return
    try:
        ensure_target_can_be_moderated(chat_id, target["id"])
    except TelegramError as exc:
        api("sendMessage", chat_id=chat_id, text=str(exc))
        return
    with database() as connection:
        inserted, count, status = record_report(
            connection,
            chat_id=chat_id,
            message_id=replied["message_id"],
            target_user_id=target["id"],
            target_name=display_name(target),
            reporter_id=reporter["id"],
            now=int(time.time()),
        )
        if status == "rate_limited":
            api("sendMessage", chat_id=chat_id, text="Report limit reached. Try again later.")
            return
        if status not in {"open", "auto_muted"}:
            api("sendMessage", chat_id=chat_id, text=f"This report case is already {status}.")
            return
        if not inserted:
            api("sendMessage", chat_id=chat_id, text="You already reported this message.")
            return
        api(
            "sendMessage",
            chat_id=chat_id,
            text=f"Report recorded ({count}/{REPORT_THRESHOLD} for moderator review).",
        )
        case = get_case(connection, chat_id, replied["message_id"])
        if case and count == REPORT_THRESHOLD and case["alert_message_id"] is None:
            alert_id = send_moderation_alert(chat_id, case)
            set_case_status(connection, chat_id, replied["message_id"], "open", alert_message_id=alert_id)
            case = get_case(connection, chat_id, replied["message_id"])
        if case and count >= AUTO_MUTE_THRESHOLD and case["status"] == "open":
            mute_user(chat_id, target["id"])
            set_case_status(connection, chat_id, replied["message_id"], "auto_muted")
            audit(connection, chat_id, 0, target["id"], "auto_mute", replied["message_id"])
            api(
                "sendMessage",
                chat_id=chat_id,
                text=(
                    f"{display_name(target)} was automatically muted for {TEMP_MUTE_HOURS} hour(s) "
                    f"after {count} unique reports. An administrator must confirm any permanent ban."
                ),
            )


def handle_ban(message: dict[str, Any]) -> None:
    if not require_admin(message):
        return
    replied = message.get("reply_to_message")
    chat_id = message["chat"]["id"]
    if not replied or not replied.get("from"):
        api("sendMessage", chat_id=chat_id, text="Reply to the offending message with /ban.")
        return
    target = replied["from"]
    try:
        ensure_target_can_be_moderated(chat_id, target["id"])
        api("banChatMember", chat_id=chat_id, user_id=target["id"], revoke_messages=True)
    except TelegramError as exc:
        api("sendMessage", chat_id=chat_id, text=str(exc))
        return
    with database() as connection:
        case = get_case(connection, chat_id, replied["message_id"])
        if case:
            set_case_status(connection, chat_id, replied["message_id"], "banned")
        audit(connection, chat_id, message["from"]["id"], target["id"], "ban", replied["message_id"])
    api("sendMessage", chat_id=chat_id, text=f"Banned {display_name(target)} by administrator decision.")


def handle_unban(message: dict[str, Any], arguments: list[str]) -> None:
    if not require_admin(message):
        return
    chat_id = message["chat"]["id"]
    if not arguments or not arguments[0].lstrip("-").isdigit():
        api("sendMessage", chat_id=chat_id, text="Usage: /unban USER_ID")
        return
    user_id = int(arguments[0])
    api("unbanChatMember", chat_id=chat_id, user_id=user_id, only_if_banned=True)
    with database() as connection:
        audit(connection, chat_id, message["from"]["id"], user_id, "unban", None)
    api("sendMessage", chat_id=chat_id, text=f"Unbanned user {user_id}. They may join again using an invite link.")


def handle_reports(message: dict[str, Any]) -> None:
    if not require_admin(message):
        return
    chat_id = message["chat"]["id"]
    with database() as connection:
        rows = connection.execute(
            """
            SELECT c.message_id, c.target_name, c.status, COUNT(r.reporter_id) AS report_count
            FROM cases c LEFT JOIN reports r
              ON r.chat_id = c.chat_id AND r.message_id = c.message_id
            WHERE c.chat_id = ? AND c.status IN ('open', 'auto_muted')
            GROUP BY c.chat_id, c.message_id ORDER BY c.updated_at DESC LIMIT 10
            """,
            (chat_id,),
        ).fetchall()
    if not rows:
        text = "No open report cases."
    else:
        text = "Open report cases:\n" + "\n".join(
            f"Message {row['message_id']}: {row['target_name']} — {row['report_count']} reports ({row['status']})"
            for row in rows
        )
    api("sendMessage", chat_id=chat_id, text=text)


def handle_callback(query: dict[str, Any]) -> None:
    callback_id = query.get("id")
    message = query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    actor_id = query.get("from", {}).get("id")
    data = (query.get("data") or "").split(":")
    if len(data) != 3 or data[0] != "mod" or not chat_id:
        if callback_id:
            api("answerCallbackQuery", callback_query_id=callback_id, text="Invalid action.")
        return
    if not is_admin(chat_id, actor_id):
        api("answerCallbackQuery", callback_query_id=callback_id, text="Administrators only.", show_alert=True)
        return
    action = data[1]
    try:
        reported_message_id = int(data[2])
    except ValueError:
        api("answerCallbackQuery", callback_query_id=callback_id, text="Invalid report.")
        return
    with database() as connection:
        case = get_case(connection, chat_id, reported_message_id)
        if not case or case["status"] not in {"open", "auto_muted"}:
            api("answerCallbackQuery", callback_query_id=callback_id, text="This case is already closed.")
            return
        try:
            ensure_target_can_be_moderated(chat_id, case["target_user_id"])
            if action == "ban":
                api(
                    "banChatMember",
                    chat_id=chat_id,
                    user_id=case["target_user_id"],
                    revoke_messages=True,
                )
                status = "banned"
            elif action == "mute":
                mute_user(chat_id, case["target_user_id"])
                status = "muted"
            elif action == "dismiss":
                status = "dismissed"
            else:
                raise TelegramError("Unknown moderation action.")
        except TelegramError as exc:
            api("answerCallbackQuery", callback_query_id=callback_id, text=str(exc), show_alert=True)
            return
        set_case_status(connection, chat_id, reported_message_id, status)
        audit(connection, chat_id, actor_id, case["target_user_id"], status, reported_message_id)
    api("answerCallbackQuery", callback_query_id=callback_id, text=f"Case {status}.")
    api(
        "editMessageText",
        chat_id=chat_id,
        message_id=message["message_id"],
        text=(
            f"Moderation case {status}\n"
            f"User: {case['target_name']}\nReports: {case['report_count']}\n"
            f"Decision by admin user {actor_id}."
        ),
    )


def process_updates(state: dict[str, Any], *, poll_timeout: int = 0) -> None:
    updates = api(
        "getUpdates",
        request_timeout=poll_timeout + 10 if poll_timeout else 20,
        offset=state.get("update_offset", 0),
        timeout=poll_timeout,
        limit=100,
        allowed_updates=["message", "edited_message", "callback_query", "my_chat_member"],
    )
    for update in updates:
        state["update_offset"] = update["update_id"] + 1
        if update.get("callback_query"):
            handle_callback(update["callback_query"])
            save_state(state)
            continue
        message = update.get("message") or update.get("edited_message")
        if not message:
            save_state(state)
            continue
        command, arguments = parse_command(message)
        if command == "/setup":
            setup_chat(message, state)
        elif command == "/status":
            chat_id = message.get("chat", {}).get("id")
            entry = state.get("chats", {}).get(str(chat_id))
            if entry:
                api("sendMessage", chat_id=chat_id, text=f"Group ID: {chat_id}\nCurrent mode: {entry.get('mode')}")
        elif command == "/report":
            handle_report(message)
        elif command == "/ban":
            handle_ban(message)
        elif command == "/unban":
            handle_unban(message, arguments)
        elif command == "/reports":
            handle_reports(message)
        save_state(state)


def apply_mode(chat_id: int, entry: dict[str, Any], mode: str) -> None:
    if entry.get("mode") == mode:
        return
    permissions = dict(entry.get("day_permissions", {}))
    if mode == "locked":
        for field in SEND_PERMISSION_FIELDS:
            permissions[field] = False
    api(
        "setChatPermissions",
        chat_id=chat_id,
        permissions=permissions,
        use_independent_chat_permissions=True,
    )
    entry["mode"] = mode
    entry["updated_at"] = datetime.now(TIMEZONE).isoformat()
    LOG.info("Applied %s mode to chat %s (%s)", mode, chat_id, entry.get("title", ""))


def reconcile_quiet_hours(state: dict[str, Any]) -> None:
    mode = desired_mode(datetime.now(TIMEZONE))
    for chat_id, entry in state.get("chats", {}).items():
        try:
            apply_mode(int(chat_id), entry, mode)
        except TelegramError:
            LOG.exception("Could not update chat %s", chat_id)
    save_state(state)


def register_commands() -> None:
    api(
        "setMyCommands",
        commands=[
            {"command": "report", "description": "Reply to report a recent message"},
            {"command": "ban", "description": "Admin: reply to ban a user"},
            {"command": "unban", "description": "Admin: unban by user ID"},
            {"command": "reports", "description": "Admin: list open report cases"},
            {"command": "status", "description": "Show quiet-hours status"},
            {"command": "setup", "description": "Admin: configure this group"},
        ],
    )


def tick() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with TICK_LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        state = load_state()
        process_updates(state)
        reconcile_quiet_hours(state)


def daemon() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with DAEMON_LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info("Daemon is already running")
            return
        with database():
            pass
        register_commands()
        state = load_state()
        LOG.info("Long-poll daemon started")
        while True:
            try:
                process_updates(state, poll_timeout=25)
                reconcile_quiet_hours(state)
            except TelegramError:
                LOG.exception("Telegram loop failed; retrying")
                time.sleep(5)
            except Exception:
                LOG.exception("Unexpected daemon failure; retrying")
                time.sleep(5)


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Telegram Group Night Guard")
    subcommands = parser.add_subparsers(dest="command")
    configure = subcommands.add_parser("configure", help="Configure a group by numeric chat ID")
    configure.add_argument("chat_id", type=int)
    subcommands.add_parser("daemon", help="Run continuous long polling")
    args = parser.parse_args()
    if args.command == "configure":
        state = load_state()
        configure_chat(args.chat_id, state)
        entry = state["chats"][str(args.chat_id)]
        print(f"Configured {entry['title']} ({args.chat_id}); mode={entry['mode']}")
    elif args.command == "daemon":
        daemon()
    else:
        tick()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("Bot failed")
        sys.exit(1)
