import tempfile
import time
import unittest
from pathlib import Path

from app.flood_gate import EDIT, MEDIA, FloodGate, describe_wait


class FloodGateTests(unittest.TestCase):
    def test_new_gate_is_not_blocked(self) -> None:
        gate = FloodGate()
        self.assertEqual(gate.remaining(MEDIA), 0)
        self.assertEqual(gate.remaining(EDIT), 0)

    def test_note_blocks_the_bucket_only(self) -> None:
        gate = FloodGate()
        wait = gate.note(MEDIA, 600)
        self.assertGreater(wait, 590)
        self.assertGreater(gate.remaining(MEDIA), 590)
        self.assertEqual(gate.remaining(EDIT), 0)

    def test_note_never_shortens_an_existing_wait(self) -> None:
        gate = FloodGate()
        gate.note(MEDIA, 3600)
        gate.note(MEDIA, 60)
        self.assertGreater(gate.remaining(MEDIA), 3500)

    def test_note_extends_when_telegram_asks_for_longer(self) -> None:
        gate = FloodGate()
        gate.note(MEDIA, 60)
        gate.note(MEDIA, 7200)
        self.assertGreater(gate.remaining(MEDIA), 7100)

    def test_zero_or_missing_seconds_changes_nothing(self) -> None:
        gate = FloodGate()
        self.assertEqual(gate.note(MEDIA, 0), 0)
        self.assertEqual(gate.note(MEDIA, None), 0)
        self.assertEqual(gate.remaining(MEDIA), 0)

    def test_note_error_reads_the_seconds_attribute(self) -> None:
        class FakeFlood(Exception):
            seconds = 120

        gate = FloodGate()
        self.assertGreater(gate.note_error(FakeFlood(), MEDIA), 110)
        self.assertGreater(gate.remaining(MEDIA), 110)

    def test_state_survives_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flood.json"
            first = FloodGate(path)
            first.note(MEDIA, 900)
            second = FloodGate(path)
            self.assertGreater(second.remaining(MEDIA), 850)
            self.assertEqual(second.remaining(EDIT), 0)

    def test_a_fraction_of_a_second_still_blocks(self) -> None:
        gate = FloodGate()
        gate._until[MEDIA] = time.time() + 0.2
        self.assertEqual(gate.remaining(MEDIA), 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flood.json"
            path.write_text('{"media": %f, "edit": 0}' % (time.time() - 5))
            gate = FloodGate(path)
            self.assertEqual(gate.remaining(MEDIA), 0)

    def test_corrupt_state_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flood.json"
            path.write_text("not json")
            gate = FloodGate(path)
            self.assertEqual(gate.remaining(MEDIA), 0)

    def test_describe_wait_rounds_up_and_splits_hours(self) -> None:
        self.assertEqual(describe_wait(0), "稍后")
        self.assertEqual(describe_wait(1), "1 分钟")
        self.assertEqual(describe_wait(60), "1 分钟")
        self.assertEqual(describe_wait(61), "2 分钟")
        self.assertEqual(describe_wait(3600), "1 小时")
        self.assertEqual(describe_wait(4800), "1 小时 20 分钟")


if __name__ == "__main__":
    unittest.main()
