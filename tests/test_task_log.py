import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.access_control import AccessControl
from app.task_log import TaskLog, file_key, today_key


class _Doc:
    def __init__(self, file_id: int) -> None:
        self.id = file_id


class _Msg:
    def __init__(self, file_id: int | None) -> None:
        self.document = _Doc(file_id) if file_id else None


class TaskLogTests(unittest.TestCase):
    def test_reuse_record_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = TaskLog(Path(directory) / "task_log.json")
            self.assertIsNone(log.lookup("9"))
            log.remember(
                "9",
                source_chat_id=1,
                source_message_id=2,
                image_chat_id=1,
                image_message_id=3,
            )
            reloaded = TaskLog(Path(directory) / "task_log.json")
            record = reloaded.lookup("9")
            self.assertEqual(record["source_message_id"], 2)
            self.assertEqual(record["image_message_id"], 3)

    def test_guest_quota_resets_next_day_and_reuse_does_not_consume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = TaskLog(Path(directory) / "task_log.json", daily_limit=10)
            day = datetime(2026, 10, 15, 12, tzinfo=timezone(timedelta(hours=8)))
            for _ in range(10):
                self.assertTrue(log.can_generate(7, day))
                log.consume(7, day)
            self.assertFalse(log.can_generate(7, day))
            self.assertEqual(log.used_today(7, day), 10)
            next_day = day + timedelta(days=1)
            self.assertTrue(log.can_generate(7, next_day))
            result = log.check_in(7, day, roll=lambda _a, _b: 4)
            self.assertEqual(result, (4, 4))
            self.assertIsNone(log.check_in(7, day, roll=lambda _a, _b: 9))
            self.assertTrue(log.can_generate(7, day))
            log.consume(7, day)
            self.assertEqual(log.stored_credits(7), 3)
            self.assertEqual(log.used_today(7, day), 10)
            self.assertEqual(file_key(_Msg(42)), "42")
            self.assertIsNone(file_key(_Msg(None)))
            self.assertEqual(today_key(day), "2026-10-15")

    def test_other_user_matches_by_name_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = TaskLog(Path(directory) / "task_log.json")
            log.remember(
                "111",
                source_chat_id=5,
                source_message_id=6,
                image_chat_id=5,
                image_message_id=7,
                content_key="clip.mp4|1000|12",
            )
            found = log.lookup("999", "clip.mp4|1000|12")
            self.assertEqual(found["image_message_id"], 7)
            self.assertEqual(found["source_chat_id"], 5)

    def test_grant_ignores_open_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            access = AccessControl(
                Path(directory) / "access.json",
                frozenset({1}),
                open_access=True,
            )
            self.assertTrue(access.has_grant(1))
            self.assertFalse(access.has_grant(2))
            self.assertTrue(access.is_allowed(2))


if __name__ == "__main__":
    unittest.main()
