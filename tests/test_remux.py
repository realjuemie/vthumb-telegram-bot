import unittest
from pathlib import Path

from app.remux import evaluate_copy_remux, remux_copy_args


class CopyRemuxTests(unittest.TestCase):
    def test_h264_aac_is_allowed(self) -> None:
        ok, reason = evaluate_copy_remux(
            [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ]
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_h264_without_audio_is_allowed(self) -> None:
        ok, _reason = evaluate_copy_remux([{"codec_type": "video", "codec_name": "h264"}])
        self.assertTrue(ok)

    def test_hevc_is_rejected(self) -> None:
        ok, reason = evaluate_copy_remux(
            [
                {"codec_type": "video", "codec_name": "hevc"},
                {"codec_type": "audio", "codec_name": "aac"},
            ]
        )
        self.assertFalse(ok)
        self.assertIn("hevc", reason)
        self.assertIn("H.264", reason)

    def test_opus_audio_is_rejected(self) -> None:
        ok, reason = evaluate_copy_remux(
            [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "opus"},
            ]
        )
        self.assertFalse(ok)
        self.assertIn("opus", reason)

    def test_ffmpeg_args_copy_only(self) -> None:
        args = remux_copy_args(Path("in.mkv"), Path("out.mp4"))
        self.assertEqual(args[0], "ffmpeg")
        self.assertIn("-c", args)
        self.assertEqual(args[args.index("-c") + 1], "copy")
        self.assertIn("+faststart", args)
        joined = " ".join(args)
        self.assertNotIn("libx264", joined)
        self.assertNotIn("-crf", joined)
