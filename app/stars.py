"""Optional Telegram Stars packs. Off unless an admin turns the gate on.

A missing gate file means off, so a fresh checkout does not sell anything.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

PACKS: dict[str, dict[str, int]] = {
    "s1": {"stars": 15, "credits": 10},
    "s2": {"stars": 60, "credits": 50},
    "s3": {"stars": 150, "credits": 150},
}


class StarsGate:
    """Admin switch. Default off. Not a slash command."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.enabled = self._load()
        self._lock_held = False

    def _load(self) -> bool:
        if not self.path.exists():
            return False
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        return bool(payload.get("enabled", False))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({"enabled": self.enabled}) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    async def set_enabled(self, enabled: bool) -> bool:
        changed = self.enabled != bool(enabled)
        self.enabled = bool(enabled)
        self._save()
        return changed


class StarsLedger:
    """One row per Telegram charge. Remaining credits decide if that charge can be refunded."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.charges = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        charges = payload.get("charges") if isinstance(payload, dict) else None
        return charges if isinstance(charges, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"charges": self.charges}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def known(self, charge_id: str) -> bool:
        return charge_id in self.charges

    def grant(self, charge_id: str, user_id: int, pack_id: str, stars: int, credits: int) -> bool:
        """Return True only the first time this charge is recorded."""
        if not charge_id or charge_id in self.charges:
            return False
        token = secrets.token_hex(4)
        while any(item.get("token") == token for item in self.charges.values()):
            token = secrets.token_hex(4)
        self.charges[charge_id] = {
            "user_id": int(user_id),
            "pack": pack_id,
            "stars": int(stars),
            "credits": int(credits),
            "remaining": int(credits),
            "token": token,
            "refunded": False,
        }
        self._save()
        return True

    def token_for(self, charge_id: str) -> str | None:
        item = self.charges.get(charge_id)
        if not item or item.get("refunded"):
            return None
        return str(item.get("token") or "") or None

    def by_token(self, token: str) -> tuple[str, dict] | None:
        for charge_id, item in self.charges.items():
            if item.get("token") == token:
                return charge_id, item
        return None

    def note_spend(self, user_id: int, count: int = 1) -> None:
        left = int(count)
        ordered = [
            (charge_id, item)
            for charge_id, item in self.charges.items()
            if int(item.get("user_id") or 0) == int(user_id) and not item.get("refunded")
        ]
        changed = False
        for _charge_id, item in ordered:
            if left <= 0:
                break
            remaining = int(item.get("remaining") or 0)
            if remaining <= 0:
                continue
            take = min(remaining, left)
            item["remaining"] = remaining - take
            spent = list(item.get("spent_stack") or [])
            spent.extend([_charge_id] * take)
            item["spent_stack"] = spent[-32:]
            left -= take
            changed = True
        if changed:
            self._save()

    def note_unspend(self, user_id: int, count: int = 1) -> None:
        left = int(count)
        ordered = [
            item
            for charge_id, item in reversed(list(self.charges.items()))
            if int(item.get("user_id") or 0) == int(user_id) and not item.get("refunded")
        ]
        changed = False
        for item in ordered:
            if left <= 0:
                break
            stack = list(item.get("spent_stack") or [])
            credits = int(item.get("credits") or 0)
            remaining = int(item.get("remaining") or 0)
            while left > 0 and stack and remaining < credits:
                stack.pop()
                remaining += 1
                left -= 1
                changed = True
            item["spent_stack"] = stack
            item["remaining"] = remaining
        if changed:
            self._save()

    def can_refund(self, charge_id: str, user_id: int) -> bool:
        item = self.charges.get(charge_id)
        if not item or item.get("refunded"):
            return False
        if int(item.get("user_id") or 0) != int(user_id):
            return False
        return int(item.get("remaining") or 0) == int(item.get("credits") or 0) and int(item.get("credits") or 0) > 0

    def mark_refunded(self, charge_id: str) -> dict | None:
        item = self.charges.get(charge_id)
        if not item or item.get("refunded"):
            return None
        item["refunded"] = True
        item["remaining"] = 0
        self._save()
        return item


def pack_from_payload(payload: bytes | str | None) -> tuple[str, dict[str, int]] | None:
    if isinstance(payload, bytes):
        try:
            pack_id = payload.decode("ascii")
        except UnicodeDecodeError:
            return None
    else:
        pack_id = str(payload or "")
    pack = PACKS.get(pack_id)
    if pack is None:
        return None
    return pack_id, pack


def precheck_error(payload: bytes, currency: str, total_amount: int, *, enabled: bool) -> str | None:
    """None means the checkout may proceed. Otherwise a short error for Telegram."""
    if not enabled:
        return "Payments are turned off."
    found = pack_from_payload(payload)
    if found is None or currency != "XTR" or int(total_amount) != found[1]["stars"]:
        return "This pack is no longer available."
    return None


async def send_pack_invoice(client, user_id: int, pack_id: str, title: str, description: str) -> None:
    import random

    from telethon import functions, types

    pack = PACKS[pack_id]
    peer = await client.get_input_entity(int(user_id))
    await client(
        functions.messages.SendMediaRequest(
            peer=peer,
            media=types.InputMediaInvoice(
                title=title,
                description=description,
                invoice=types.Invoice(
                    currency="XTR",
                    prices=[types.LabeledPrice(label=title, amount=pack["stars"])],
                ),
                payload=pack_id.encode("ascii"),
                provider="",
                provider_data=types.DataJSON(data="{}"),
            ),
            message="",
            random_id=random.randrange(1, 2**63),
        )
    )


async def refund_charge(client, user_id: int, charge_id: str) -> None:
    from telethon import functions, types

    entity = await client.get_input_entity(int(user_id))
    if not isinstance(entity, types.InputPeerUser):
        raise RuntimeError("not a user")
    await client(
        functions.payments.RefundStarsChargeRequest(
            user_id=types.InputUser(entity.user_id, entity.access_hash),
            charge_id=charge_id,
        )
    )
