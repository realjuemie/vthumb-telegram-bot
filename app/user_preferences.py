import asyncio
import json
import os
import tempfile
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
THEME_POTPLAYER = "potplayer"
THEME_BLACK_BG = "black_bg"
THEME_WHITE_BG = "white_bg"
THEME_PURE_IMAGE = "pure_image"
THEME_MINIMAL_BLACK = "minimal_black"
THEME_MINIMAL_WHITE = "minimal_white"
THEMES = (
    THEME_POTPLAYER,
    THEME_BLACK_BG,
    THEME_WHITE_BG,
    THEME_PURE_IMAGE,
    THEME_MINIMAL_BLACK,
    THEME_MINIMAL_WHITE,
)
THEME_LABELS = {
    THEME_POTPLAYER: "PotPlayer 风格",
    THEME_BLACK_BG: "黑底白字",
    THEME_WHITE_BG: "白底黑字",
    THEME_PURE_IMAGE: "纯图无信息",
    THEME_MINIMAL_BLACK: "极简模式：黑",
    THEME_MINIMAL_WHITE: "极简模式：白",
}
DEFAULT_THEME = THEME_POTPLAYER


class UserPreferences:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.values, self.delivery_modes, self.theme_modes = self._load()
        self.lock = asyncio.Lock()

    def _load(self) -> tuple[dict[int, ThumbnailPreset], dict[int, str], dict[int, str]]:
        if not self.path.exists():
            return {}, {}, {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            values: dict[int, ThumbnailPreset] = {}
            delivery_modes: dict[int, str] = {}
            theme_modes: dict[int, str] = {}
            for user_id, item in payload.get("preferences", {}).items():
                preset = PRESETS_BY_KEY.get((int(item["count"]), int(item["cols"])))
                if preset:
                    values[int(user_id)] = preset
                delivery = item.get("delivery", DEFAULT_DELIVERY)
                if delivery in DELIVERY_MODES:
                    delivery_modes[int(user_id)] = delivery
                theme = item.get("theme", DEFAULT_THEME)
                if theme in THEMES:
                    theme_modes[int(user_id)] = theme
            return values, delivery_modes, theme_modes
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

    def get_theme(self, user_id: int | None) -> str:
        if user_id is None:
            return DEFAULT_THEME
        return self.theme_modes.get(user_id, DEFAULT_THEME)

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

    async def set_theme(self, user_id: int, theme: str) -> str:
        if theme not in THEMES:
            raise ValueError("Unsupported theme.")
        async with self.lock:
            self.theme_modes[user_id] = theme
            self._save()
        return theme

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        user_ids = sorted(set(self.values) | set(self.delivery_modes) | set(self.theme_modes))
        payload = {"preferences": {}}
        for user_id in user_ids:
            preset = self.get(user_id)
            payload["preferences"][str(user_id)] = {
                "count": preset.count,
                "cols": preset.cols,
                "delivery": self.get_delivery(user_id),
                "theme": self.get_theme(user_id),
            }
        # Atomically write via a tempfile *owned by the current user*.
        # `self.path.with_suffix('.tmp')` would not work when the bind-
        # mounted data dir was created by the container bootstrap (root)
        #         and the bot process runs as non-root -- the stale .tmp file
        #         would be owned by root and `write_text` / `replace()` would
        #         raise PermissionError, silently breaking every preference change.
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=".preferences.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
