import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PATH = Path(__file__).with_name("quiet_hours_bot.py")
SPEC = importlib.util.spec_from_file_location("quiet_hours_bot", PATH)
BOT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(BOT)


class QuietHoursTests(unittest.TestCase):
    def test_quiet_hours_boundaries(self):
        timezone = ZoneInfo("Asia/Yangon")
        cases = (
            (22, 59, "unlocked"),
            (23, 0, "locked"),
            (8, 59, "locked"),
            (9, 0, "unlocked"),
        )
        for hour, minute, expected in cases:
            with self.subTest(hour=hour, minute=minute):
                now = datetime(2026, 6, 19, hour, minute, tzinfo=timezone)
                self.assertEqual(BOT.desired_mode(now), expected)

    def test_parse_addressed_command(self):
        command, arguments = BOT.parse_command({"text": "/unban@night_guard_bot 12345"})
        self.assertEqual(command, "/unban")
        self.assertEqual(arguments, ["12345"])

    def test_reports_are_unique_per_reporter(self):
        with tempfile.TemporaryDirectory() as directory:
            original_app_dir = BOT.APP_DIR
            original_database_file = BOT.DATABASE_FILE
            try:
                BOT.APP_DIR = Path(directory)
                BOT.DATABASE_FILE = Path(directory) / "moderation.db"
                with BOT.database() as connection:
                    first = BOT.record_report(
                        connection,
                        chat_id=-1001,
                        message_id=10,
                        target_user_id=20,
                        target_name="Reported User",
                        reporter_id=30,
                        now=1_000_000,
                    )
                    duplicate = BOT.record_report(
                        connection,
                        chat_id=-1001,
                        message_id=10,
                        target_user_id=20,
                        target_name="Reported User",
                        reporter_id=30,
                        now=1_000_001,
                    )
                    second = BOT.record_report(
                        connection,
                        chat_id=-1001,
                        message_id=10,
                        target_user_id=20,
                        target_name="Reported User",
                        reporter_id=31,
                        now=1_000_002,
                    )
                self.assertEqual(first[:2], (True, 1))
                self.assertEqual(duplicate[:2], (False, 1))
                self.assertEqual(second[:2], (True, 2))
            finally:
                BOT.APP_DIR = original_app_dir
                BOT.DATABASE_FILE = original_database_file

    def test_callback_data_fits_telegram_limit(self):
        keyboard = BOT.moderation_keyboard(2_147_483_647)
        for row in keyboard["inline_keyboard"]:
            for button in row:
                self.assertLessEqual(len(button["callback_data"].encode()), 64)

    def test_reminder_is_sent_once_per_slot(self):
        original_api = BOT.api
        original_save_state = BOT.save_state
        original_times = BOT.REMINDER_TIMES
        calls = []

        def fake_api(method, **params):
            calls.append((method, params))
            return {"username": "test_guard_bot"} if method == "getMe" else {"message_id": 1}

        try:
            BOT.api = fake_api
            BOT.save_state = lambda state: None
            BOT.REMINDER_TIMES = ("09:30",)
            state = {"chats": {"-1001": {"title": "Test"}}}
            now = datetime(2026, 6, 21, 9, 30, tzinfo=ZoneInfo("Asia/Yangon"))
            BOT.send_due_reminders(state, now)
            BOT.send_due_reminders(state, now)
            sent_messages = [params for method, params in calls if method == "sendMessage"]
            self.assertEqual(len(sent_messages), 1)
            self.assertIn("/report@test_guard_bot", sent_messages[0]["text"])
            self.assertEqual(state["last_reminder_message_ids"]["-1001"], 1)
        finally:
            BOT.api = original_api
            BOT.save_state = original_save_state
            BOT.REMINDER_TIMES = original_times

    def test_new_reminder_deletes_previous_reminder(self):
        original_api = BOT.api
        original_save_state = BOT.save_state
        calls = []

        def fake_api(method, **params):
            calls.append((method, params))
            return {"message_id": 200}

        try:
            BOT.api = fake_api
            BOT.save_state = lambda state: None
            state = {"last_reminder_message_ids": {"-1001": 100}}
            result = BOT.send_reminder(state, -1001, "test_guard_bot")
            self.assertEqual(result, 200)
            self.assertEqual([method for method, _ in calls], ["deleteMessage", "sendMessage"])
            self.assertEqual(calls[0][1]["message_id"], 100)
            self.assertEqual(state["last_reminder_message_ids"]["-1001"], 200)
        finally:
            BOT.api = original_api
            BOT.save_state = original_save_state


if __name__ == "__main__":
    unittest.main()
