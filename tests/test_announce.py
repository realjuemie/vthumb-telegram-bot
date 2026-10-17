import tempfile
import unittest
from pathlib import Path

from app.announce import Audience, announce_body, collect_recipients


class AnnounceTests(unittest.TestCase):
    def test_body_keeps_markdown_and_newlines(self) -> None:
        raw = "/announce **标题**\n第二行 [链接](https://example.com)"
        self.assertEqual(announce_body(raw), "**标题**\n第二行 [链接](https://example.com)")
        self.assertEqual(announce_body("/announce"), "")

    def test_audience_remembers_users(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audience.json"
            audience = Audience(path)
            audience.note(7)
            audience.note(7)
            audience.add_many([8, "bad", 0])
            reloaded = Audience(path)
            self.assertEqual(reloaded.ids(), [7, 8])

    def test_recipients_skip_publisher(self) -> None:
        self.assertEqual(collect_recipients([1, 2, 2], [3]), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
