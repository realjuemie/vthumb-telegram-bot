import asyncio
import unittest

from app.bot import PROGRESS_EDIT_INTERVAL, ProgressReporter


class _FakeClient:
    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_message(self, chat_id, message_id, text) -> None:
        self.edits.append(text)


class ProgressEditThrottleTests(unittest.IsolatedAsyncioTestCase):
    def _reporter(self, client) -> ProgressReporter:
        reporter = ProgressReporter(client, 1, 2)
        reporter.task.cancel()
        return reporter

    async def test_edits_inside_the_interval_are_skipped(self) -> None:
        client = _FakeClient()
        reporter = self._reporter(client)
        try:
            await reporter.task
        except asyncio.CancelledError:
            pass

        reporter._set_state("frames", 1, 10)
        await reporter._flush()
        self.assertEqual(len(client.edits), 1)

        reporter._set_state("frames", 2, 10)
        await reporter._flush()
        self.assertEqual(len(client.edits), 1, "second edit inside the interval must be dropped")

        reporter._set_state("frames", 3, 10)
        await reporter._flush()
        self.assertEqual(len(client.edits), 1)

    async def test_forced_updates_bypass_the_interval(self) -> None:
        client = _FakeClient()
        reporter = self._reporter(client)
        try:
            await reporter.task
        except asyncio.CancelledError:
            pass

        reporter._set_state("frames", 1, 10)
        await reporter._flush()
        self.assertEqual(len(client.edits), 1)

        await reporter.show_now("upload", 1, 4)
        self.assertEqual(len(client.edits), 2, "a stage change should show immediately")
        self.assertIn("正在上传成品图片", client.edits[-1])

    async def test_interval_is_not_faster_than_the_old_loop(self) -> None:
        self.assertGreaterEqual(PROGRESS_EDIT_INTERVAL, 2.0)


if __name__ == "__main__":
    unittest.main()
