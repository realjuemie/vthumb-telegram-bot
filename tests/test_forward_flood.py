import tempfile
import unittest
from pathlib import Path

from telethon.errors import FloodWaitError

from app import bot as bot_module
from app.access_control import AccessControl
from app.bot import _forward_flush, _pending_forward
from app.flood_gate import MEDIA, FloodGate


class _ForwardClient:
    def __init__(self, fail_file: bool = True) -> None:
        self.calls: list[str] = []
        self.fail_file = fail_file

    async def send_message(self, chat_id, text, parse_mode=None):
        self.calls.append("text")

    async def send_file(self, chat_id, media):
        self.calls.append("file")
        if self.fail_file:
            raise FloodWaitError(request=None, capture=800)


class ForwardFloodTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._saved = bot_module._flood
        bot_module._flood = FloodGate()
        _pending_forward.clear()

    def tearDown(self) -> None:
        bot_module._flood = self._saved
        _pending_forward.clear()

    def _access(self, tmp: str) -> AccessControl:
        return AccessControl(Path(tmp) / "access.json", frozenset({7}))

    async def test_flood_on_the_album_does_not_fall_back_item_by_item(self) -> None:
        client = _ForwardClient()
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "sheet.png"
            png.write_bytes(b"png")
            _pending_forward[3] = {
                "items": [{"png": str(png), "message": object()}],
                "label": "user",
                "dirs": [],
            }
            await _forward_flush(client, self._access(tmp), 3)

        self.assertEqual(client.calls, ["text", "file"])
        self.assertGreater(bot_module._flood.remaining(MEDIA), 790)
        self.assertIn(3, _pending_forward)

    async def test_a_closed_send_gate_makes_no_network_call(self) -> None:
        bot_module._flood.note(MEDIA, 800)
        client = _ForwardClient(fail_file=False)
        with tempfile.TemporaryDirectory() as tmp:
            _pending_forward[3] = {"items": [], "label": "user", "dirs": []}
            await _forward_flush(client, self._access(tmp), 3)

        self.assertEqual(client.calls, [])
        self.assertIn(3, _pending_forward)


if __name__ == "__main__":
    unittest.main()
