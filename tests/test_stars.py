import tempfile
import unittest
from pathlib import Path

from app.stars import StarsGate, StarsLedger, precheck_error
from app.task_log import TaskLog
from datetime import datetime, timedelta, timezone


class StarsTests(unittest.TestCase):
    def test_gate_missing_file_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate = StarsGate(Path(directory) / "stars_gate.json")
            self.assertFalse(gate.enabled)

    def test_charge_is_granted_once_and_unused_can_refund(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = StarsLedger(Path(directory) / "stars_payments.json")
            self.assertTrue(ledger.grant("c1", 7, "s1", 15, 10))
            self.assertFalse(ledger.grant("c1", 7, "s1", 15, 10))
            self.assertTrue(ledger.can_refund("c1", 7))
            ledger.note_spend(7, 1)
            self.assertFalse(ledger.can_refund("c1", 7))
            ledger.note_unspend(7, 1)
            self.assertTrue(ledger.can_refund("c1", 7))

    def test_precheck_rejects_when_off_or_amount_mismatches(self) -> None:
        self.assertEqual(precheck_error(b"s1", "XTR", 15, enabled=False), "Payments are turned off.")
        self.assertIsNone(precheck_error(b"s1", "XTR", 15, enabled=True))
        self.assertIsNotNone(precheck_error(b"s1", "XTR", 1, enabled=True))

    def test_gift_is_spent_before_sponsor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = TaskLog(Path(directory) / "task_log.json", daily_limit=1)
            day = datetime(2026, 11, 22, 12, tzinfo=timezone(timedelta(hours=8)))
            log.consume(7, day)
            log.add_sponsor(7, 2)
            log.add_stored_many([7], 3)
            log.consume(7, day)
            self.assertEqual(log.gift_credits(7), 2)
            self.assertEqual(log.sponsor_credits(7), 2)
            self.assertEqual(log.refund(7, day), "gift")
            self.assertEqual(log.gift_credits(7), 3)
