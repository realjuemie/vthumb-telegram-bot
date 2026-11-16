import unittest
from types import SimpleNamespace

from app import bot as bot_module
from app.bot import JobQueue, QueueTicket
from app.flood_gate import EDIT, FloodGate


class _QueueClient:
    def __init__(self) -> None:
        self.edits: list[tuple[int, int, str]] = []

    async def edit_message(self, chat_id, message_id, text) -> None:
        self.edits.append((chat_id, message_id, text))


class QueueNoticeThrottleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._saved = bot_module._flood
        bot_module._flood = FloodGate()

    def tearDown(self) -> None:
        bot_module._flood = self._saved

    def _ticket(self, client, chat_id: int, message_id: int, last_ahead=None) -> QueueTicket:
        ticket = QueueTicket(client, chat_id, "video.mp4")
        ticket.status_id = message_id
        ticket.last_ahead = last_ahead
        return ticket

    async def test_unchanged_positions_are_not_edited_again(self) -> None:
        client = _QueueClient()
        queue = JobQueue(1)
        tickets = [self._ticket(client, 7, 100 + i) for i in range(3)]

        await queue._notify_waiters(tickets, 1)
        self.assertEqual(len(client.edits), 3)
        self.assertEqual([text.splitlines()[1] for _, _, text in client.edits],
                         ["前面还有 1 个任务", "前面还有 2 个任务", "前面还有 3 个任务"])

        await queue._notify_waiters(tickets, 1)
        self.assertEqual(len(client.edits), 3, "same positions must not be re-sent")

    async def test_a_new_joiner_does_not_re_edit_the_queue_ahead(self) -> None:
        client = _QueueClient()
        queue = JobQueue(1)
        waiting = [self._ticket(client, 7, 100 + i, last_ahead=i + 1) for i in range(5)]
        newcomer = self._ticket(client, 7, 200)

        await queue._notify_waiters(waiting + [newcomer], 1)

        self.assertEqual(len(client.edits), 1, "only the newcomer changes")
        self.assertEqual(client.edits[0][1], 200)

    async def test_nothing_is_edited_while_the_edit_bucket_is_blocked(self) -> None:
        client = _QueueClient()
        queue = JobQueue(1)
        tickets = [self._ticket(client, 7, 100)]
        bot_module._flood.note(EDIT, 600)

        await queue._notify_waiters(tickets, 1)
        self.assertEqual(client.edits, [])


if __name__ == "__main__":
    unittest.main()
