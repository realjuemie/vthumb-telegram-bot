import asyncio
import json
from pathlib import Path


class AccessControl:
    def __init__(self, path: Path, admin_ids: frozenset[int], open_access: bool = False) -> None:
        if not admin_ids:
            raise RuntimeError("At least one ADMIN_IDS entry is required.")
        self.path = path
        self.admin_ids = admin_ids
        self.open_access = open_access
        self.user_ids = self._load()
        self.lock = asyncio.Lock()

    def _load(self) -> set[int]:
        if not self.path.exists():
            return set()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            users = payload.get("users", [])
            return {int(user_id) for user_id in users if int(user_id) > 0}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError(f"Cannot read access list: {self.path}") from exc

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids

    def is_allowed(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        if self.open_access:
            return True
        return self.is_admin(user_id) or user_id in self.user_ids

    def has_grant(self, user_id: int | None) -> bool:
        """Explicit admin or allow-list membership. Open access does not count."""
        if user_id is None:
            return False
        return self.is_admin(user_id) or user_id in self.user_ids

    def users(self) -> list[int]:
        return sorted(self.user_ids)

    async def add(self, user_id: int) -> bool:
        async with self.lock:
            if user_id in self.user_ids or user_id in self.admin_ids:
                return False
            self.user_ids.add(user_id)
            self._save()
            return True

    async def remove(self, user_id: int) -> bool:
        async with self.lock:
            if user_id not in self.user_ids:
                return False
            self.user_ids.remove(user_id)
            self._save()
            return True

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"users": sorted(self.user_ids)}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


class GroupAccess:
    """Groups where granted users may request thumbnails."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.chat_ids = self._load()
        self.lock = asyncio.Lock()

    def _load(self) -> set[int]:
        if not self.path.exists():
            return set()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            chats = payload.get("chats", [])
            return {int(chat_id) for chat_id in chats}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError(f"Cannot read group list: {self.path}") from exc

    def is_authorized(self, chat_id: int | None) -> bool:
        return chat_id is not None and int(chat_id) in self.chat_ids

    def chats(self) -> list[int]:
        return sorted(self.chat_ids)

    async def add(self, chat_id: int) -> bool:
        async with self.lock:
            if chat_id in self.chat_ids:
                return False
            self.chat_ids.add(int(chat_id))
            self._save()
            return True

    async def remove(self, chat_id: int) -> bool:
        async with self.lock:
            if chat_id not in self.chat_ids:
                return False
            self.chat_ids.remove(int(chat_id))
            self._save()
            return True

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"chats": sorted(self.chat_ids)}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
