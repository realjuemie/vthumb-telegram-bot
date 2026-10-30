"""Admin announcements: draft, markdown preview, then publish."""
from __future__ import annotations

import json
from pathlib import Path


def private_sender_id(is_private: bool, sender_id: int | None) -> int | None:
    """Only private chats belong in the announcement list."""
    if not is_private or sender_id is None:
        return None
    try:
        value = int(sender_id)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def announce_body(raw: str | None) -> str:
    text = raw or ""
    parts = text.split(None, 1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def collect_recipients(*groups) -> list[int]:
    found: set[int] = set()
    for group in groups:
        for item in group or []:
            try:
                user_id = int(item)
            except (TypeError, ValueError):
                continue
            if user_id > 0:
                found.add(user_id)
    return sorted(found)


class Audience:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.user_ids = self._load()

    def _load(self) -> set[int]:
        if not self.path.exists():
            return set()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return set()
        users = payload.get("users") if isinstance(payload, dict) else None
        if not isinstance(users, list):
            return set()
        return {int(item) for item in users if str(item).isdigit() and int(item) > 0}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"users": sorted(self.user_ids)}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def note(self, user_id: int | None) -> None:
        if user_id is None or user_id <= 0 or user_id in self.user_ids:
            return
        self.user_ids.add(int(user_id))
        self._save()

    def add_many(self, user_ids) -> None:
        before = len(self.user_ids)
        for user_id in user_ids:
            try:
                value = int(user_id)
            except (TypeError, ValueError):
                continue
            if value > 0:
                self.user_ids.add(value)
        if len(self.user_ids) != before:
            self._save()

    def discard(self, user_id: int) -> None:
        if int(user_id) not in self.user_ids:
            return
        self.user_ids.discard(int(user_id))
        self._save()

    def replace(self, user_ids) -> None:
        found: set[int] = set()
        for user_id in user_ids or []:
            try:
                value = int(user_id)
            except (TypeError, ValueError):
                continue
            if value > 0:
                found.add(value)
        if found == self.user_ids:
            return
        self.user_ids = found
        self._save()

    def ids(self) -> list[int]:
        return sorted(self.user_ids)
