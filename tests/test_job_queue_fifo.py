import asyncio
import unittest

from app.bot import JobQueue


class _UnevenClient:
    """The queued job named slow waits on its notice; fast does not."""

    def __init__(self) -> None:
        self.sends: list[str] = []

    async def send_message(self, chat_id, text, reply_to=None):
        if "slow.mp4" in text:
            await asyncio.sleep(0.3)
        self.sends.append(text)
        return type("Msg", (), {"id": len(self.sends)})()

    async def delete_messages(self, chat_id, ids):
        return None

    async def edit_message(self, chat_id, message_id, text):
        return None


class FifoDespiteSlowNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_slow_queue_notice_cannot_be_overtaken(self) -> None:
        queue = JobQueue(1)
        client = _UnevenClient()
        started: list[str] = []

        async def hold() -> None:
            await queue.join(client, 1, 1, "hold.mp4")
            await asyncio.sleep(0.05)
            await queue.leave()

        async def queued(name: str) -> None:
            await queue.join(client, 1, 1, name)
            started.append(name)
            await queue.leave()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0.01)
        slow = asyncio.create_task(queued("slow.mp4"))
        await asyncio.sleep(0.01)
        fast = asyncio.create_task(queued("fast.mp4"))
        await asyncio.gather(holder, slow, fast)
        self.assertEqual(started, ["slow.mp4", "fast.mp4"])


if __name__ == "__main__":
    unittest.main()
