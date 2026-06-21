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


if __name__ == "__main__":
    unittest.main()
