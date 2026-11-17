"""Durable leftovers that must survive a restart.

Admin forwards used to live only in memory, so a restart dropped them and
the temp copies vanished with them. Announcements used to start over, so a
FloodWait in the middle resent the people who had already received it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path


class ForwardStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[int, dict]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            logging.warning("Could not read pending forwards; starting empty")
            return {}
        if not isinstance(raw, dict):
            return {}
        pending: dict[int, dict] = {}
        for key, state in raw.items():
            try:
                user_id = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(state, dict):
                continue
            items = []
            for item in state.get("items") or []:
                if not isinstance(item, dict):
                    continue
                items.append({
                    "png": item.get("png"),
                    "source_chat_id": item.get("source_chat_id"),
                    "source_message_id": item.get("source_message_id"),
                    "message": None,
                })
            pending[user_id] = {
                "items": items,
                "label": state.get("label") or str(user_id),
                "sent_admins": list(state.get("sent_admins") or []),
                "dirs": [],
                "task": None,
            }
        return pending

    def save(self, pending: dict[int, dict]) -> None:
        payload = {}
        for user_id, state in pending.items():
            items = []
            for item in state.get("items") or []:
                message = item.get("message")
                chat_id = item.get("source_chat_id")
                message_id = item.get("source_message_id")
                if chat_id is None and message is not None:
                    chat_id = getattr(message, "chat_id", None)
                if message_id is None and message is not None:
                    message_id = getattr(message, "id", None)
                items.append({
                    "png": item.get("png"),
                    "source_chat_id": chat_id,
                    "source_message_id": message_id,
                })
            payload[str(int(user_id))] = {
                "label": state.get("label") or str(user_id),
                "sent_admins": list(state.get("sent_admins") or []),
                "items": items,
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)


class AnnounceLedger:
    """Who already received the current announcement text."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.text = ""
        self.sent: set[int] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict):
            return
        self.text = str(raw.get("text") or "")
        self.sent = {int(item) for item in raw.get("sent") or [] if str(item).lstrip("-").isdigit()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"text": self.text, "sent": sorted(self.sent)}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def already_sent(self, text: str) -> set[int]:
        if text != self.text:
            return set()
        return set(self.sent)

    def mark(self, text: str, user_id: int) -> None:
        if text != self.text:
            self.text = text
            self.sent = set()
        self.sent.add(int(user_id))
        self._save()
