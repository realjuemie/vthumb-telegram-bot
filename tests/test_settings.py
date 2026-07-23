import unittest

from app.settings import Settings


def make_settings() -> Settings:
    return Settings(
        bot_token="token",
        api_id=1,
        api_hash="hash",
        thumb_width=1920,
        max_concurrent_jobs=1,
        ffmpeg_timeout=90,
        range_cache_mb=128,
        max_source_fetch_mb=256,
        max_source_fetch_ratio=0.35,
        hard_source_fetch_ratio=0.55,
        min_source_fetch_mb=32,
        small_file_full_read_mb=64,
        source_fetch_growth_mb=16,
        mt_proxy_url=None,
        admin_ids=frozenset(),
        access_file="access.json",
        preferences_file="preferences.json",
    )


class SourceFetchBudgetTests(unittest.TestCase):
    def test_small_file_can_read_full_range(self) -> None:
        settings = make_settings()
        file_size = 20 * 1024 * 1024

        self.assertEqual(settings.source_fetch_budget(file_size), file_size)
        self.assertEqual(settings.source_fetch_hard_budget(file_size), file_size)

    def test_file_above_small_threshold_keeps_hard_guard(self) -> None:
        settings = make_settings()
        mib = 1024 * 1024
        file_size = 65 * mib

        self.assertEqual(settings.source_fetch_budget(file_size), 32 * mib)
        self.assertEqual(settings.source_fetch_hard_budget(file_size), 64 * mib)
        self.assertLess(settings.source_fetch_hard_budget(file_size), file_size)

    def test_large_file_uses_adaptive_ratio(self) -> None:
        settings = make_settings()
        mib = 1024 * 1024
        file_size = 200 * mib

        self.assertEqual(settings.source_fetch_budget(file_size), 70 * mib)
        self.assertEqual(settings.source_fetch_hard_budget(file_size), 110 * mib)

    def test_absolute_cap_still_applies(self) -> None:
        settings = make_settings()
        mib = 1024 * 1024
        file_size = 1024 * mib

        self.assertEqual(settings.source_fetch_budget(file_size), 256 * mib)
        self.assertEqual(settings.source_fetch_hard_budget(file_size), 256 * mib)


if __name__ == "__main__":
    unittest.main()
