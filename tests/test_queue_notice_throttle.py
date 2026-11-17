import asyncio
import unittest

from app import bot as bot_module
from app.bot import JobQueue
from app.flood_gate import EDIT, MEDIA, FloodGate


class _QueueClient:
    def __init__(self) -> None:
        self.sends: list[str] = []
        self.edits: list[tuple[int, int, str]] = []
        self._n = 0

    async def send_message(self, chat_id, text, reply_to=None):
        self._n += 1
        self.sends.append(text)
        return type("Msg", (), {"id": self._n})()

    async def edit_message(self, chat_id, message_id, text) -> None:
        self.edits.append((chat_id, message_id, text))

    async def delete_messages(self, chat_id, ids) -> None:
        return None


class QueueBatchNoticeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._saved = bot_module._flood
        bot_module._flood = FloodGate()

    def tearDown(self) -> None:
        bot_module._flood = self._saved

    async def test_a_burst_in_one_chat_sends_one_summary(self) -> None:
        queue = JobQueue(1)
        queue.batch_quiet = 0.02
        client = _QueueClient()

        async def hold() -> None:
            await queue.join(client, 7, 1, "hold.mp4")
            await asyncio.sleep(0.2)
            await queue.leave()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0.01)
        waiting = []
        for i in range(8):
            waiting.append(asyncio.create_task(queue.join(client, 7, 10 + i, f"{i}.mp4")))
            await asyncio.sleep(0)
        await queue._flush_batch(7)
        self.assertEqual(len(client.sends), 1)
        self.assertIn("后面还有 8 个", client.sends[0])
        self.assertEqual(client.edits, [])
        holder.cancel()
        for task in waiting:
            task.cancel()
        await asyncio.gather(holder, *waiting, return_exceptions=True)

    async def test_same_count_is_not_edited_again(self) -> None:
        queue = JobQueue(1)
        queue.batch_quiet = 0.01
        client = _QueueClient()
        queue._batches[7] = {
            "waiting": 3,
            "client": client,
            "reply_to": 1,
            "quote_top_id": None,
            "peer": None,
            "task": None,
            "message_id": 50,
            "last_text": bot_module.format_queue_batch(3),
        }
        await queue._flush_batch(7)
        self.assertEqual(client.edits, [])

    async def test_summary_is_not_sent_while_sends_are_blocked(self) -> None:
        queue = JobQueue(1)
        queue.batch_quiet = 0.01
        client = _QueueClient()
        bot_module._flood.note(MEDIA, 600)
        queue._batches[7] = {
            "waiting": 4,
            "client": client,
            "reply_to": 1,
            "quote_top_id": None,
            "peer": None,
            "task": None,
            "message_id": None,
            "last_text": "",
        }
        await queue._flush_batch(7)
        self.assertEqual(client.sends, [])

    async def test_summary_edit_waits_while_the_edit_bucket_is_blocked(self) -> None:
        queue = JobQueue(1)
        client = _QueueClient()
        bot_module._flood.note(EDIT, 600)
        queue._batches[7] = {
            "waiting": 2,
            "client": client,
            "reply_to": 1,
            "quote_top_id": None,
            "peer": None,
            "task": None,
            "message_id": 50,
            "last_text": "old",
        }
        await queue._flush_batch(7)
        self.assertEqual(client.edits, [])


if __name__ == "__main__":
    unittest.main()
