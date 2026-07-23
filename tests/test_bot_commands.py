import unittest

from app.bot import (
    BOT_COMMANDS,
    command_name,
    command_user_id,
    delivery_from_callback,
    format_progress,
    setting_from_callback,
)


class BotCommandTests(unittest.TestCase):
    def test_extracts_plain_command(self) -> None:
        self.assertEqual(command_name("/status"), "status")

    def test_extracts_addressed_command(self) -> None:
        self.assertEqual(command_name("/help@killfatsbot extra"), "help")

    def test_menu_commands_are_unique_and_lowercase(self) -> None:
        names = [name for name, _ in BOT_COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(name.isascii() and name.islower() for name in names))

    def test_extracts_add_user_id(self) -> None:
        self.assertEqual(command_user_id("/add 123456789"), 123456789)

    def test_rejects_invalid_user_id(self) -> None:
        self.assertIsNone(command_user_id("/add not-a-number"))

    def test_frame_progress_contains_count_and_percent(self) -> None:
        text = format_progress("frames", 4, 16)
        self.assertIn("4/16", text)
        self.assertIn("25%", text)

    def test_upload_progress_reaches_one_hundred_percent(self) -> None:
        self.assertIn("100%", format_progress("upload", 1024, 1024))

    def test_setting_callback_resolves_preset(self) -> None:
        preset = setting_from_callback(b"setting:20:5")
        self.assertIsNotNone(preset)
        self.assertEqual((preset.count, preset.cols, preset.rows), (20, 5, 4))

    def test_setting_callback_rejects_unknown_preset(self) -> None:
        self.assertIsNone(setting_from_callback(b"setting:24:6"))

    def test_delivery_callback_accepts_media_and_file(self) -> None:
        self.assertEqual(delivery_from_callback(b"delivery:media"), "media")
        self.assertEqual(delivery_from_callback(b"delivery:file"), "file")

    def test_delivery_callback_rejects_unknown_mode(self) -> None:
        self.assertIsNone(delivery_from_callback(b"delivery:other"))


if __name__ == "__main__":
    unittest.main()
