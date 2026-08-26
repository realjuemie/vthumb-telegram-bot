import tempfile
import unittest
from pathlib import Path

from app.vthumb import (
    _probe_duration,
    ffmpeg_frame_args,
    sample_time,
    trim_trailing_duplicates,
)


class ProbeDurationTests(unittest.TestCase):
    def test_prefers_shorter_stream_duration(self) -> None:
        duration = _probe_duration(
            {"duration": "120.0"},
            {"duration": "63.5", "avg_frame_rate": "30/1", "nb_frames": "1905"},
        )
        self.assertAlmostEqual(duration, 63.5, places=2)

    def test_uses_nb_frames_when_shorter(self) -> None:
        duration = _probe_duration(
            {"duration": "200"},
            {"avg_frame_rate": "25/1", "nb_frames": "2500"},
        )
        self.assertAlmostEqual(duration, 100.0, places=2)

    def test_ignores_zero_and_invalid(self) -> None:
        duration = _probe_duration({"duration": "0"}, {"duration": "N/A", "avg_frame_rate": "0/0"})
        self.assertEqual(duration, 0.0)


class FrameArgsTests(unittest.TestCase):
    def test_input_seek_then_decode_window(self) -> None:
        args = ffmpeg_frame_args(
            "http://127.0.0.1/media/x",
            Path("/tmp/f.jpg"),
            input_ss=10.0,
            output_ss=8.0,
        )
        ss_at = [i for i, item in enumerate(args) if item == "-ss"]
        self.assertEqual(args[ss_at[0] + 1], "10.000")
        self.assertEqual(args[ss_at[1] + 1], "8.000")
        self.assertIn("-strict", args)
        self.assertIn("-1", args)
        self.assertIn("-an", args)

    def test_sample_time_first_frame_is_early(self) -> None:
        first = sample_time(170.0, 1, 16)
        second = sample_time(170.0, 2, 16)
        self.assertLess(first, second)
        self.assertAlmostEqual(first, 3.4, places=2)


class TrailingDuplicateTests(unittest.TestCase):
    def test_drops_identical_tail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            frames = []
            times = []
            for index, payload in enumerate((b"A", b"B", b"C", b"C", b"C"), start=1):
                path = tmp / f"{index}.jpg"
                path.write_bytes(payload)
                frames.append(path)
                times.append(float(index))
            kept_frames, kept_times = trim_trailing_duplicates(frames, times)
            self.assertEqual([p.read_bytes() for p in kept_frames], [b"A", b"B", b"C"])
            self.assertEqual(kept_times, [1.0, 2.0, 3.0])

    def test_keeps_all_when_unique(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            frames = []
            times = []
            for index in range(4):
                path = tmp / f"{index}.jpg"
                path.write_bytes(bytes([index + 10]))
                frames.append(path)
                times.append(float(index))
            kept_frames, kept_times = trim_trailing_duplicates(frames, times)
            self.assertEqual(kept_frames, frames)
            self.assertEqual(kept_times, times)


if __name__ == "__main__":
    unittest.main()
