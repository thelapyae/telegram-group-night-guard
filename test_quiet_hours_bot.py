import importlib.util
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


if __name__ == "__main__":
    unittest.main()
