import os
from dataclasses import dataclass
from urllib.parse import urlparse


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_set_env(name: str) -> frozenset[int]:
    values = os.getenv(name, "")
    return frozenset(int(value.strip()) for value in values.split(",") if value.strip())


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_id: int
    api_hash: str
    thumb_width: int
    max_concurrent_jobs: int
    ffmpeg_timeout: int
    range_cache_mb: int
    max_source_fetch_mb: int
    max_source_fetch_ratio: float
    hard_source_fetch_ratio: float
    min_source_fetch_mb: int
    small_file_full_read_mb: int
    source_fetch_growth_mb: int
    mt_proxy_url: str | None
    admin_ids: frozenset[int]
    open_access: bool
    access_file: str
    preferences_file: str

    @property
    def mt_proxy(self) -> dict[str, object] | None:
        if not self.mt_proxy_url:
            return None
        parsed = urlparse(self.mt_proxy_url)
        if not parsed.hostname or not parsed.port:
            raise RuntimeError("MT_PROXY_URL must include a host and port.")
        return {
            "proxy_type": parsed.scheme or "http",
            "addr": parsed.hostname,
            "port": parsed.port,
            "username": parsed.username,
            "password": parsed.password,
            "rdns": True,
        }

    def source_fetch_budget(self, file_size: int) -> int:
        mib = 1024 * 1024
        if file_size <= self.small_file_full_read_mb * mib:
            return file_size
        ratio_budget = max(self.min_source_fetch_mb * mib, int(file_size * self.max_source_fetch_ratio))
        return min(file_size, self.max_source_fetch_mb * mib, ratio_budget)

    def source_fetch_hard_budget(self, file_size: int) -> int:
        mib = 1024 * 1024
        if file_size <= self.small_file_full_read_mb * mib:
            return file_size
        ratio_budget = max(
            self.small_file_full_read_mb * mib,
            int(file_size * self.hard_source_fetch_ratio),
        )
        return min(file_size, self.max_source_fetch_mb * mib, ratio_budget)

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is required.")
        api_id = _int_env("TG_API_ID", 0)
        api_hash = os.getenv("TG_API_HASH", "").strip()
        if not api_id or not api_hash:
            raise RuntimeError("TG_API_ID and TG_API_HASH are required for MTProto range reading.")

        return cls(
            bot_token=token,
            api_id=api_id,
            api_hash=api_hash,
            thumb_width=_int_env("THUMB_WIDTH", 1920),
            max_concurrent_jobs=_int_env("MAX_CONCURRENT_JOBS", 1),
            ffmpeg_timeout=_int_env("FFMPEG_TIMEOUT", 90),
            range_cache_mb=_int_env("RANGE_CACHE_MB", 128),
            max_source_fetch_mb=_int_env("MAX_SOURCE_FETCH_MB", 256),
            max_source_fetch_ratio=_float_env("MAX_SOURCE_FETCH_RATIO", 0.35),
            hard_source_fetch_ratio=_float_env("HARD_SOURCE_FETCH_RATIO", 0.55),
            min_source_fetch_mb=_int_env("MIN_SOURCE_FETCH_MB", 32),
            small_file_full_read_mb=_int_env("SMALL_FILE_FULL_READ_MB", 64),
            source_fetch_growth_mb=_int_env("SOURCE_FETCH_GROWTH_MB", 16),
            mt_proxy_url=os.getenv("MT_PROXY_URL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"),
            admin_ids=_int_set_env("ADMIN_IDS"),
            open_access=_bool_env("OPEN_ACCESS", False),
            access_file=os.getenv("ACCESS_FILE", "/data/access.json"),
            preferences_file=os.getenv("PREFERENCES_FILE", "/data/preferences.json"),
        )
