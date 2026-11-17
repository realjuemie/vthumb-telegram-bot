"""Honour Telegram FloodWait instead of retrying into a longer ban.

Telegram answers a rate-limited request with a fixed wait. Retrying that
request right away pushes the deadline further out: on 2026-11-16 an
800 s SendMediaRequest wait grew to roughly 13 hours over one evening of
immediate retries. The state is persisted so a service restart does not
clear a wait that Telegram still enforces.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime
from pathlib import Path

MEDIA = "media"
EDIT = "edit"
BUCKETS = (MEDIA, EDIT)


class FloodGate:
    """Per-bucket FloodWait deadlines, in memory and on disk."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._until: dict[str, float] = {bucket: 0.0 for bucket in BUCKETS}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text())
        except Exception:
            # A half-written state file just means "no known wait".
            logging.warning("Could not read flood state; starting clear")
            return
        if not isinstance(data, dict):
            return
        for bucket in BUCKETS:
            try:
                self._until[bucket] = float(data.get(bucket) or 0)
            except (TypeError, ValueError):
                pass

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(self._until))
            os.replace(tmp, self.path)
        except Exception:
            logging.warning("Could not persist flood state", exc_info=True)
            try:
                tmp.unlink()
            except OSError:
                pass

    def remaining(self, bucket: str = MEDIA) -> int:
        """Seconds still to wait. A fraction of a second still counts as blocked."""
        with self._lock:
            left = self._until.get(bucket, 0.0) - time.time()
        if left <= 0:
            return 0
        return math.ceil(left)

    def note(self, bucket: str, seconds: float | int | None) -> int:
        """Record a FloodWait of `seconds`; never shortens an existing wait."""
        try:
            wait = float(seconds or 0)
        except (TypeError, ValueError):
            return self.remaining(bucket)
        if wait <= 0:
            return self.remaining(bucket)
        with self._lock:
            deadline = time.time() + wait
            if deadline > self._until.get(bucket, 0.0):
                self._until[bucket] = deadline
                self._save()
            return max(0, math.ceil(self._until[bucket] - time.time()))

    def note_error(self, exc: BaseException, bucket: str = MEDIA) -> int:
        return self.note(bucket, getattr(exc, "seconds", 0))


def describe_wait(seconds: int) -> str:
    """`45 分钟` / `1 小时 20 分钟`, for messages to the user."""
    if seconds <= 0:
        return "稍后"
    minutes = (int(seconds) + 59) // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    if rest:
        return f"{hours} 小时 {rest} 分钟"
    return f"{hours} 小时"


def resume_clock(seconds: int, now: float | None = None) -> str:
    """Local clock when the wait ends, for the user-facing notice."""
    moment = time.time() if now is None else now
    clear_at = moment + max(0, int(seconds))
    when = datetime.fromtimestamp(clear_at)
    if datetime.fromtimestamp(moment).date() == when.date():
        return when.strftime("%H:%M")
    return when.strftime("%m-%d %H:%M")
