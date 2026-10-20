import unittest

from app.mp4_index import materialize, parse_atom_header, patch_chunk_offsets, plan_faststart


def _atom(typ: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + typ + payload


def _stco(offsets: list[int]) -> bytes:
    body = b"\x00\x00\x00\x00" + len(offsets).to_bytes(4, "big")
    body += b"".join(item.to_bytes(4, "big") for item in offsets)
    return _atom(b"stco", body)


class Mp4IndexTests(unittest.TestCase):
    def test_moves_moov_before_mdat_and_shifts_chunk_offsets(self) -> None:
        ftyp = _atom(b"ftyp", b"isom")
        sample = b"FRAME"
        mdat = _atom(b"mdat", sample)
        sample_at = len(ftyp) + 8
        moov = _atom(b"moov", _atom(b"mvhd", b"\x00" * 8) + _stco([sample_at]))
        blob = ftyp + mdat + moov
        layout = plan_faststart(len(blob), lambda off, n: blob[off:off + n])
        self.assertIsNotNone(layout)
        assert layout is not None
        logical = materialize(layout, lambda off, n: blob[off:off + n])
        self.assertEqual(len(logical), len(blob))
        self.assertEqual(logical[4:8], b"ftyp")
        self.assertLess(logical.find(b"moov"), logical.find(b"mdat"))
        self.assertIn(sample, logical)
        shifted = sample_at + len(moov)
        self.assertIn(shifted.to_bytes(4, "big"), logical)

    def test_faststart_file_is_left_alone(self) -> None:
        ftyp = _atom(b"ftyp", b"isom")
        moov = _atom(b"moov", _atom(b"mvhd", b"\x00" * 4) + _stco([20]))
        mdat = _atom(b"mdat", b"FRAME")
        blob = ftyp + moov + mdat
        self.assertIsNone(plan_faststart(len(blob), lambda off, n: blob[off:off + n]))

    def test_patch_rejects_a_truncated_count(self) -> None:
        broken = (20).to_bytes(4, "big") + b"stco" + b"\x00" * 12
        self.assertEqual(patch_chunk_offsets(broken, 8), broken)

    def test_header_rejects_a_size_past_eof(self) -> None:
        self.assertIsNone(parse_atom_header((100).to_bytes(4, "big") + b"ftyp", 16, 0))


if __name__ == "__main__":
    unittest.main()
