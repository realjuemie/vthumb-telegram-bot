"""Successful generation records and the guest daily quota."""
from __future__ import annotations

import json
import random
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
        # `alias` is filename|size|duration. Different videos can share that,
        # so it must not select a saved sheet. Callers still pass it.
        del alias
        if not key:
            return None
        record = self.files.get(key)
        if not isinstance(record, dict):
            return None
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
        # Do not also index by filename|size|duration. That alias is not a fingerprint.
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

    def _normalize(self, entry: dict | None) -> dict:
        entry = dict(entry or {})
        if "checkin_credits" not in entry and "gift_credits" not in entry:
            try:
                legacy = max(0, int(entry.pop("stored", 0) or 0))
            except (TypeError, ValueError):
                legacy = 0
                entry.pop("stored", None)
            # The old pool mixed check-in and admin grants. The live totals are
            # mostly grants, so the unsplit remainder is shown as a system gift.
            entry["checkin_credits"] = 0
            entry["gift_credits"] = legacy
        else:
            entry.pop("stored", None)
            for field in ("checkin_credits", "gift_credits"):
                try:
                    entry[field] = max(0, int(entry.get(field) or 0))
                except (TypeError, ValueError):
                    entry[field] = 0
        try:
            entry["sponsor_credits"] = max(0, int(entry.get("sponsor_credits") or 0))
        except (TypeError, ValueError):
            entry["sponsor_credits"] = 0
        stack = entry.get("spend")
        if not isinstance(stack, list):
            entry["spend"] = []
        else:
            entry["spend"] = [item for item in stack if item in {"daily", "checkin", "sponsor", "gift"}][-32:]
        return entry

    def _get(self, user_id: int) -> dict:
        return self._normalize(self.quota.get(str(user_id)) or {})

    def used_today(self, user_id: int, now: datetime | None = None) -> int:
        entry = self.quota.get(str(user_id)) or {}
        if entry.get("date") != today_key(now):
            return 0
        try:
            return int(entry.get("count") or 0)
        except (TypeError, ValueError):
            return 0

    def checkin_credits(self, user_id: int) -> int:
        return self._get(user_id)["checkin_credits"]

    def gift_credits(self, user_id: int) -> int:
        return self._get(user_id)["gift_credits"]

    def sponsor_credits(self, user_id: int) -> int:
        return self._get(user_id)["sponsor_credits"]

    def stored_credits(self, user_id: int) -> int:
        entry = self._get(user_id)
        return entry["checkin_credits"] + entry["gift_credits"] + entry["sponsor_credits"]

    def checked_in_on(self, user_id: int, now: datetime | None = None) -> bool:
        entry = self.quota.get(str(user_id)) or {}
        return entry.get("checkin") == today_key(now)

    def check_in(self, user_id: int, now: datetime | None = None, roll=None) -> tuple[int, int] | None:
        """Grant 1-3 stored credits once per day. None if already checked in."""
        if self.checked_in_on(user_id, now):
            return None
        gained = int((roll or random.randint)(1, 3))
        if gained < 1 or gained > 3:
            raise ValueError("check-in reward must be from 1 to 3")
        key = str(user_id)
        day = today_key(now)
        entry = self._get(user_id)
        if entry.get("date") != day:
            entry["date"] = day
            entry["count"] = 0
        entry["checkin_credits"] = entry["checkin_credits"] + gained
        entry["checkin"] = day
        self.quota[key] = entry
        self._save()
        return gained, entry["checkin_credits"]

    def can_generate(self, user_id: int, now: datetime | None = None) -> bool:
        return self.used_today(user_id, now) < self.daily_limit or self.stored_credits(user_id) > 0

    def consume(self, user_id: int, now: datetime | None = None) -> int:
        key = str(user_id)
        day = today_key(now)
        entry = self._get(user_id)
        count = int(entry.get("count") or 0) if entry.get("date") == day else 0
        if count < self.daily_limit:
            count += 1
            bucket = "daily"
        elif entry["checkin_credits"] > 0:
            entry["checkin_credits"] -= 1
            bucket = "checkin"
        elif entry["gift_credits"] > 0:
            entry["gift_credits"] -= 1
            bucket = "gift"
        elif entry["sponsor_credits"] > 0:
            entry["sponsor_credits"] -= 1
            bucket = "sponsor"
        else:
            count += 1
            bucket = "daily"
        entry["date"] = day
        entry["count"] = count
        stack = list(entry.get("spend") or [])
        stack.append(bucket)
        entry["spend"] = stack[-32:]
        self.quota[key] = entry
        self._save()
        return count

    def add_sponsor(self, user_id: int, amount: int) -> int:
        if amount < 1:
            raise ValueError("amount must be positive")
        key = str(int(user_id))
        entry = self._get(int(user_id))
        entry["sponsor_credits"] = entry["sponsor_credits"] + int(amount)
        self.quota[key] = entry
        self._save()
        return entry["sponsor_credits"]

    def remove_sponsor(self, user_id: int, amount: int) -> int:
        key = str(int(user_id))
        entry = self._get(int(user_id))
        entry["sponsor_credits"] = max(0, entry["sponsor_credits"] - int(amount))
        self.quota[key] = entry
        self._save()
        return entry["sponsor_credits"]

    def refund(self, user_id: int, now: datetime | None = None) -> str | None:
        """Give back the last slot taken by `consume`, for a job that produced nothing."""
        key = str(user_id)
        day = today_key(now)
        entry = self._get(user_id)
        stack = list(entry.get("spend") or [])
        bucket = stack[-1] if stack else None
        if bucket == "daily":
            if entry.get("date") != day or int(entry.get("count") or 0) <= 0:
                return None
            entry["count"] = int(entry.get("count") or 0) - 1
            stack.pop()
        elif bucket == "checkin":
            entry["checkin_credits"] += 1
            stack.pop()
        elif bucket == "sponsor":
            entry["sponsor_credits"] += 1
            stack.pop()
        elif bucket == "gift":
            entry["gift_credits"] += 1
            stack.pop()
        elif entry.get("date") == day and int(entry.get("count") or 0) > 0:
            entry["count"] = int(entry.get("count") or 0) - 1
            bucket = "daily"
        elif entry.get("date") == day:
            entry["gift_credits"] += 1
            bucket = "gift"
        else:
            return None
        entry["spend"] = stack
        self.quota[key] = entry
        self._save()
        return bucket

    def add_stored_many(self, user_ids, amount: int) -> int:
        """Add system-gift credits. Does not touch today's free allowance or check-in."""
        if amount < 1:
            raise ValueError("amount must be positive")
        count = 0
        for user_id in user_ids:
            key = str(int(user_id))
            entry = self._get(int(user_id))
            entry["gift_credits"] = entry["gift_credits"] + int(amount)
            self.quota[key] = entry
            count += 1
        if count:
            self._save()
        return count
