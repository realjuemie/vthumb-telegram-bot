import asyncio
import json
from pathlib import Path


class AccessControl:
    def __init__(self, path: Path, admin_ids: frozenset[int]) -> None:
        if not admin_ids:
            raise RuntimeError("At least one ADMIN_IDS entry is required.")
        self.path = path
        self.admin_ids = admin_ids
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
        return self.is_admin(user_id) or (user_id is not None and user_id in self.user_ids)

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
