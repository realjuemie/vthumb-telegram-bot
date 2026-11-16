import unittest

from telethon.errors import FloodWaitError

from app import bot as bot_module
from app.bot import _publish_announce
from app.flood_gate import MEDIA, FloodGate


class _AnnounceClient:
    def __init__(self, fail_at: int | None = None, wait: int = 45161) -> None:
        self.sent: list[int] = []
        self.fail_at = fail_at
        self.wait = wait

    async def send_message(self, user_id, text, parse_mode=None, link_preview=None):
        if self.fail_at is not None and len(self.sent) >= self.fail_at:
            raise FloodWaitError(request=None, capture=self.wait)
        self.sent.append(user_id)


class _Blocked(Exception):
    pass


class _Audience:
    def __init__(self, ids):
        self._ids = list(ids)
        self.dropped: list[int] = []

    def discard(self, user_id):
        self.dropped.append(user_id)
        if user_id in self._ids:
            self._ids.remove(user_id)


class AnnounceGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._saved = bot_module._flood
        bot_module._flood = FloodGate()

    def tearDown(self) -> None:
        bot_module._flood = self._saved

    async def test_nothing_is_attempted_while_sends_are_blocked(self) -> None:
        bot_module._flood.note(MEDIA, 3600)
        client = _AnnounceClient()

        result = await _publish_announce(client, "hi", [1, 2, 3])

        self.assertEqual(client.sent, [])
        self.assertEqual(result.sent, 0)
        self.assertEqual(result.unsent, 3)
        self.assertGreater(result.wait, 3500)

    async def test_a_flood_midway_stops_instead_of_hammering(self) -> None:
        client = _AnnounceClient(fail_at=2)

        result = await _publish_announce(client, "hi", [1, 2, 3, 4, 5])

        self.assertEqual(client.sent, [1, 2], "must stop at the first refusal")
        self.assertEqual(result.sent, 2)
        self.assertEqual(result.unsent, 3, "3, 4 and 5 were never attempted")
        self.assertGreater(bot_module._flood.remaining(MEDIA), 45000)

    async def test_a_clean_run_reports_everyone(self) -> None:
        client = _AnnounceClient()

        result = await _publish_announce(client, "hi", [1, 2])

        self.assertEqual(client.sent, [1, 2])
        self.assertEqual((result.sent, result.failed, result.unsent, result.wait), (2, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
