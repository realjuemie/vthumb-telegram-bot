import asyncio
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ThumbnailPreset:
    count: int
    cols: int
    rows: int

    @property
    def label(self) -> str:
        return f"{self.count}帧 {self.cols}x{self.rows}"


PRESETS = (
    ThumbnailPreset(16, 4, 4),
    ThumbnailPreset(20, 5, 4),
    ThumbnailPreset(25, 5, 5),
    ThumbnailPreset(30, 5, 6),
)
PRESETS_BY_KEY = {(preset.count, preset.cols): preset for preset in PRESETS}
DEFAULT_PRESET = PRESETS[0]
DELIVERY_MEDIA = "media"
DELIVERY_FILE = "file"
DELIVERY_MODES = {DELIVERY_MEDIA, DELIVERY_FILE}
DEFAULT_DELIVERY = DELIVERY_MEDIA


class UserPreferences:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.values, self.delivery_modes = self._load()
        self.lock = asyncio.Lock()

    def _load(self) -> tuple[dict[int, ThumbnailPreset], dict[int, str]]:
        if not self.path.exists():
            return {}, {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            values: dict[int, ThumbnailPreset] = {}
            delivery_modes: dict[int, str] = {}
            for user_id, item in payload.get("preferences", {}).items():
                preset = PRESETS_BY_KEY.get((int(item["count"]), int(item["cols"])))
                if preset:
                    values[int(user_id)] = preset
                delivery = item.get("delivery", DEFAULT_DELIVERY)
                if delivery in DELIVERY_MODES:
                    delivery_modes[int(user_id)] = delivery
            return values, delivery_modes
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise RuntimeError(f"Cannot read user preferences: {self.path}") from exc

    def get(self, user_id: int | None) -> ThumbnailPreset:
        if user_id is None:
            return DEFAULT_PRESET
        return self.values.get(user_id, DEFAULT_PRESET)

    def get_delivery(self, user_id: int | None) -> str:
        if user_id is None:
            return DEFAULT_DELIVERY
        return self.delivery_modes.get(user_id, DEFAULT_DELIVERY)

    async def set(self, user_id: int, count: int, cols: int) -> ThumbnailPreset:
        preset = PRESETS_BY_KEY.get((count, cols))
        if not preset:
            raise ValueError("Unsupported thumbnail preset.")
        async with self.lock:
            self.values[user_id] = preset
            self._save()
        return preset

    async def set_delivery(self, user_id: int, delivery: str) -> str:
        if delivery not in DELIVERY_MODES:
            raise ValueError("Unsupported delivery mode.")
        async with self.lock:
            self.delivery_modes[user_id] = delivery
            self._save()
        return delivery

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        user_ids = sorted(set(self.values) | set(self.delivery_modes))
        payload = {"preferences": {}}
        for user_id in user_ids:
            preset = self.get(user_id)
            payload["preferences"][str(user_id)] = {
                "count": preset.count,
                "cols": preset.cols,
                "delivery": self.get_delivery(user_id),
            }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
