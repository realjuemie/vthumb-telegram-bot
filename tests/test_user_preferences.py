import tempfile
import unittest
from pathlib import Path

from app.user_preferences import DEFAULT_DELIVERY, DEFAULT_PRESET, PRESETS, UserPreferences


class UserPreferencesTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_user_defaults_to_16_frames_4_by_4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preferences = UserPreferences(Path(directory) / "preferences.json")
            self.assertEqual(preferences.get(123), DEFAULT_PRESET)
            self.assertEqual((DEFAULT_PRESET.count, DEFAULT_PRESET.cols, DEFAULT_PRESET.rows), (16, 4, 4))
            self.assertEqual(preferences.get_delivery(123), "media")

    async def test_all_requested_presets_exist(self) -> None:
        values = [(preset.count, preset.cols, preset.rows) for preset in PRESETS]
        self.assertEqual(values, [(16, 4, 4), (20, 5, 4), (25, 5, 5), (30, 5, 6)])

    async def test_user_choice_is_independent_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            preferences = UserPreferences(path)
            await preferences.set(100, 20, 5)
            await preferences.set(200, 30, 5)

            reloaded = UserPreferences(path)
            self.assertEqual(reloaded.get(100).count, 20)
            self.assertEqual(reloaded.get(200).count, 30)
            self.assertEqual(reloaded.get(300), DEFAULT_PRESET)

    async def test_unsupported_preset_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preferences = UserPreferences(Path(directory) / "preferences.json")
            with self.assertRaises(ValueError):
                await preferences.set(100, 24, 6)

    async def test_delivery_choice_is_independent_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            preferences = UserPreferences(path)
            await preferences.set_delivery(100, "file")

            reloaded = UserPreferences(path)
            self.assertEqual(reloaded.get_delivery(100), "file")
            self.assertEqual(reloaded.get_delivery(200), DEFAULT_DELIVERY)

    async def test_legacy_preference_defaults_to_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            path.write_text(
                '{"preferences":{"100":{"count":20,"cols":5}}}',
                encoding="utf-8",
            )
            preferences = UserPreferences(path)
            self.assertEqual(preferences.get(100).count, 20)
            self.assertEqual(preferences.get_delivery(100), "media")

    async def test_unsupported_delivery_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preferences = UserPreferences(Path(directory) / "preferences.json")
            with self.assertRaises(ValueError):
                await preferences.set_delivery(100, "other")


if __name__ == "__main__":
    unittest.main()
