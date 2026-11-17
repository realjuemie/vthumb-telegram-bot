import tempfile
import unittest
from pathlib import Path

from app.delivery_store import AnnounceLedger, ForwardStore


class DeliveryStoreTests(unittest.TestCase):
    def test_forward_round_trip_keeps_source_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ForwardStore(Path(tmp) / "forward_outbox.json")
            message = type("M", (), {"chat_id": 9, "id": 8})()
            store.save({
                3: {
                    "label": "user",
                    "sent_admins": [1],
                    "items": [{"message": message, "png": "/data/sheet.png"}],
                }
            })
            loaded = ForwardStore(Path(tmp) / "forward_outbox.json").load()
            self.assertEqual(loaded[3]["items"][0]["source_chat_id"], 9)
            self.assertEqual(loaded[3]["items"][0]["source_message_id"], 8)
            self.assertIsNone(loaded[3]["items"][0]["message"])
            self.assertEqual(loaded[3]["sent_admins"], [1])

    def test_announce_resume_skips_people_already_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "announce_ledger.json"
            ledger = AnnounceLedger(path)
            ledger.mark("hello", 1)
            ledger.mark("hello", 2)
            again = AnnounceLedger(path)
            self.assertEqual(again.already_sent("hello"), {1, 2})
            self.assertEqual(again.already_sent("different"), set())
            again.mark("different", 3)
            self.assertEqual(again.already_sent("different"), {3})
            self.assertEqual(again.already_sent("hello"), set())


if __name__ == "__main__":
    unittest.main()
