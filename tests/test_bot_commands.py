import asyncio
import unittest

from app.bot import (
    BOT_COMMANDS,
    JobQueue,
    command_name,
    command_user_id,
    delivery_from_callback,
    format_progress,
    format_queue_status,
    format_user_label,
    setting_from_callback,
    short_filename,
)


class BotCommandTests(unittest.TestCase):
    def test_extracts_plain_command(self) -> None:
        self.assertEqual(command_name("/status"), "status")

    def test_extracts_addressed_command(self) -> None:
        self.assertEqual(command_name("/help@killfatsbot extra"), "help")

    def test_menu_commands_are_unique_and_lowercase(self) -> None:
        names = [name for name, _ in BOT_COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(name.isascii() and name.islower() for name in names))
        self.assertNotIn("forward", names)

    def test_extracts_add_user_id(self) -> None:
        self.assertEqual(command_user_id("/add 123456789"), 123456789)

    def test_rejects_invalid_user_id(self) -> None:
        self.assertIsNone(command_user_id("/add not-a-number"))

    def test_frame_progress_contains_count_and_percent(self) -> None:
        text = format_progress("frames", 4, 16)
        self.assertIn("4/16", text)
        self.assertIn("25%", text)

    def test_upload_progress_reaches_one_hundred_percent(self) -> None:
        self.assertIn("100%", format_progress("upload", 1024, 1024))

    def test_setting_callback_resolves_preset(self) -> None:
        preset = setting_from_callback(b"setting:20:5")
        self.assertIsNotNone(preset)
        self.assertEqual((preset.count, preset.cols, preset.rows), (20, 5, 4))

    def test_setting_callback_rejects_unknown_preset(self) -> None:
        self.assertIsNone(setting_from_callback(b"setting:24:6"))

    def test_delivery_callback_accepts_media_and_file(self) -> None:
        self.assertEqual(delivery_from_callback(b"delivery:media"), "media")
        self.assertEqual(delivery_from_callback(b"delivery:file"), "file")

    def test_delivery_callback_rejects_unknown_mode(self) -> None:
        self.assertIsNone(delivery_from_callback(b"delivery:other"))

    def test_queue_status_shows_ahead_count(self) -> None:
        text = format_queue_status(2, "clip.mp4")
        self.assertIn("2", text)
        self.assertIn("clip.mp4", text)
        self.assertIn("队列", text)

    def test_queue_status_ready_when_ahead_zero(self) -> None:
        self.assertEqual(format_queue_status(0, "a.mp4"), "轮到你了，开始处理。")

    def test_short_filename_truncates(self) -> None:
        long_name = "a" * 90 + ".mp4"
        self.assertTrue(short_filename(long_name).endswith("..."))
        self.assertLessEqual(len(short_filename(long_name)), 80)


class _DummyClient:
    def __init__(self) -> None:
        self.sends: list[str] = []
        self._n = 0

    async def send_message(self, chat_id, text, reply_to=None):
        self._n += 1
        self.sends.append(text)
        return type("Msg", (), {"id": self._n})()

    async def edit_message(self, chat_id, message_id, text):
        return None


class JobQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_order_and_queue_notice(self) -> None:
        queue = JobQueue(1)
        client = _DummyClient()
        started: list[str] = []

        async def job(name: str) -> None:
            await queue.join(client, 1, 1, name)
            started.append(name)
            await asyncio.sleep(0.05)
            await queue.leave()

        first = asyncio.create_task(job("one.mp4"))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(job("two.mp4"))
        await asyncio.gather(first, second)
        self.assertEqual(started, ["one.mp4", "two.mp4"])
        self.assertTrue(any("two.mp4" in text for text in client.sends))

    def test_forward_command_parses(self) -> None:
        self.assertEqual(command_name("/forward on"), "forward")

    def test_user_label_prefers_username(self) -> None:
        sender = type("U", (), {"username": "alice", "first_name": "A"})()
        self.assertEqual(format_user_label(sender, 1), "@alice")

    def test_user_label_falls_back_to_id(self) -> None:
        self.assertEqual(format_user_label(None, 42), "42")


if __name__ == "__main__":
    unittest.main()
