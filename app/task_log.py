"""Successful generation records and the guest daily quota."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))


def today_key(now: datetime | None = None) -> str:
    current = now or datetime.now(TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TZ)
    return current.astimezone(TZ).date().isoformat()


def file_key(message) -> str | None:
    document = getattr(message, "document", None)
    file_id = getattr(document, "id", None)
    if not file_id:
        return None
    return str(file_id)


def content_key(message) -> str | None:
    """Match the same video across users. A new upload has a different file id."""
    document = getattr(message, "document", None)
    if document is None:
        return None
    name = ""
    duration = 0
    for attr in getattr(document, "attributes", None) or []:
        filename = getattr(attr, "file_name", None)
        if filename:
            name = str(filename)
        if getattr(attr, "duration", None) is not None and getattr(attr, "w", None) is not None:
            duration = int(attr.duration)
    file_info = getattr(message, "file", None)
    if not name and file_info is not None:
        name = getattr(file_info, "name", None) or ""
    size = getattr(document, "size", None) or (getattr(file_info, "size", None) if file_info is not None else 0) or 0
    name = name.strip().lower()
    if not name or not size:
        return None
    return f"{name}|{int(size)}|{duration}"


class TaskLog:
    def __init__(self, path: Path, daily_limit: int = 10) -> None:
        self.path = path
        self.daily_limit = daily_limit
        self.files, self.quota = self._load()

    def _load(self) -> tuple[dict, dict]:
        if not self.path.exists():
            return {}, {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}, {}
        files = payload.get("files") if isinstance(payload.get("files"), dict) else {}
        quota = payload.get("quota") if isinstance(payload.get("quota"), dict) else {}
        return files, quota

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"files": self.files, "quota": self.quota}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def lookup(self, key: str | None, alias: str | None = None) -> dict | None:
        for candidate in (key, alias):
            if not candidate:
                continue
            record = self.files.get(candidate)
            if not isinstance(record, dict):
                continue
            if record.get("image_message_id") and record.get("source_message_id"):
                return record
        return None

    def remember(
        self,
        key: str,
        *,
        source_chat_id: int,
        source_message_id: int,
        image_chat_id: int,
        image_message_id: int,
        content_key: str | None = None,
    ) -> None:
        record = {
            "source_chat_id": int(source_chat_id),
            "source_message_id": int(source_message_id),
            "image_chat_id": int(image_chat_id),
            "image_message_id": int(image_message_id),
            "saved_at": datetime.now(TZ).isoformat(timespec="seconds"),
        }
        self.files[key] = record
        if content_key and content_key != key:
            self.files[content_key] = record
        self._save()

    def known_user_ids(self) -> set[int]:
        found: set[int] = set()
        for key in self.quota:
            try:
                user_id = int(key)
            except (TypeError, ValueError):
                continue
            if user_id > 0:
                found.add(user_id)
        return found

    def used_today(self, user_id: int, now: datetime | None = None) -> int:
        entry = self.quota.get(str(user_id)) or {}
        if entry.get("date") != today_key(now):
            return 0
        try:
            return int(entry.get("count") or 0)
        except (TypeError, ValueError):
            return 0

    def can_generate(self, user_id: int, now: datetime | None = None) -> bool:
        return self.used_today(user_id, now) < self.daily_limit

    def consume(self, user_id: int, now: datetime | None = None) -> int:
        key = str(user_id)
        day = today_key(now)
        entry = self.quota.get(key) or {}
        count = int(entry.get("count") or 0) if entry.get("date") == day else 0
        count += 1
        self.quota[key] = {"date": day, "count": count}
        self._save()
        return count
