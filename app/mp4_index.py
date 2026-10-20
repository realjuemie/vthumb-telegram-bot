"""Reorder a non-faststart MP4 so readers find the index without scanning media."""

from __future__ import annotations

from dataclasses import dataclass

MAX_MOOV_BYTES = 48 * 1024 * 1024
_CONTAINER_TYPES = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"}


@dataclass(frozen=True)
class Atom:
    offset: int
    size: int
    typ: bytes
    header: int


@dataclass(frozen=True)
class FaststartLayout:
    """Logical file is the same size, with moov moved to just before mdat."""

    file_size: int
    prefix_end: int
    moov_phys: int
    moov_size: int
    gap_phys: int
    gap_size: int
    tail_phys: int
    tail_size: int
    patched_moov: bytes

    def segments(self, start: int, end: int) -> list[tuple[str, int, int]]:
        """Cover inclusive [start, end] with ('moov', off, length) or ('file', phys, length)."""
        if start < 0 or end < start or end >= self.file_size:
            raise ValueError("logical range is outside the file")
        cursor = start
        last = end + 1
        moov_end = self.prefix_end + self.moov_size
        gap_end = moov_end + self.gap_size
        regions = (
            (0, self.prefix_end, "file", 0),
            (self.prefix_end, moov_end, "moov", 0),
            (moov_end, gap_end, "file", self.gap_phys),
            (gap_end, self.file_size, "file", self.tail_phys),
        )
        pieces: list[tuple[str, int, int]] = []
        for log_start, log_end, kind, phys_base in regions:
            if cursor >= last:
                break
            if last <= log_start or cursor >= log_end:
                continue
            left = max(cursor, log_start)
            right = min(last, log_end)
            if right <= left:
                continue
            if kind == "moov":
                pieces.append(("moov", left - log_start, right - left))
            else:
                pieces.append(("file", phys_base + (left - log_start), right - left))
            cursor = right
        if cursor != last:
            raise ValueError("logical range was not fully mapped")
        return pieces


def parse_atom_header(data: bytes, file_size: int, offset: int) -> Atom | None:
    if offset < 0 or offset + 8 > file_size or len(data) < 8:
        return None
    size = int.from_bytes(data[0:4], "big")
    typ = data[4:8]
    header = 8
    if size == 1:
        if len(data) < 16 or offset + 16 > file_size:
            return None
        size = int.from_bytes(data[8:16], "big")
        header = 16
    elif size == 0:
        size = file_size - offset
    if size < header or offset + size > file_size:
        return None
    if not all(32 <= byte < 127 for byte in typ):
        return None
    return Atom(offset, size, typ, header)


def walk_top_atoms(file_size: int, read) -> list[Atom]:
    atoms: list[Atom] = []
    offset = 0
    for _ in range(64):
        if offset + 8 > file_size:
            break
        atom = parse_atom_header(read(offset, 16), file_size, offset)
        if atom is None:
            break
        atoms.append(atom)
        if atom.typ == b"moov" and any(item.typ == b"mdat" for item in atoms):
            break
        if atom.offset + atom.size >= file_size:
            break
        offset += atom.size
    return atoms


def moov_ending_at_eof(tail: bytes, tail_offset: int, file_size: int) -> Atom | None:
    start = 0
    while True:
        found = tail.find(b"moov", start)
        if found < 0:
            return None
        start = found + 1
        if found < 4:
            continue
        header_at = tail_offset + found - 4
        size = int.from_bytes(tail[found - 4:found], "big")
        header = 8
        if size == 1 and found >= 12:
            header_at = tail_offset + found - 12
            size = int.from_bytes(tail[found - 12:found - 4], "big")
            header = 16
        if size < header or header_at < 0 or header_at + size != file_size:
            continue
        window = tail[found:found + 64]
        if b"mvhd" not in window and b"mvhd" not in tail[found:found + 256]:
            continue
        return Atom(header_at, size, b"moov", header)


def patch_chunk_offsets(moov: bytes, shift: int) -> bytes:
    if shift == 0:
        return moov
    data = bytearray(moov)

    def patch_box(pos: int, size: int, header: int, typ: bytes) -> None:
        base = pos + header
        if base + 8 > pos + size:
            return
        count = int.from_bytes(data[base + 4:base + 8], "big")
        width = 8 if typ == b"co64" else 4
        entries = base + 8
        if count < 0 or entries + count * width > pos + size:
            return
        for index in range(count):
            off = entries + index * width
            value = int.from_bytes(data[off:off + width], "big")
            data[off:off + width] = (value + shift).to_bytes(width, "big")

    def walk(start: int, end: int) -> None:
        pos = start
        while pos + 8 <= end:
            size = int.from_bytes(data[pos:pos + 4], "big")
            typ = bytes(data[pos + 4:pos + 8])
            header = 8
            if size == 1 and pos + 16 <= end:
                size = int.from_bytes(data[pos + 8:pos + 16], "big")
                header = 16
            elif size == 0:
                size = end - pos
            if size < header or pos + size > end:
                break
            if typ in (b"stco", b"co64"):
                patch_box(pos, size, header, typ)
            elif typ in _CONTAINER_TYPES:
                walk(pos + header, pos + size)
            pos += size

    walk(0, len(data))
    return bytes(data)


def layout_from_moov(file_size: int, mdat: Atom, moov: Atom, moov_bytes: bytes) -> FaststartLayout | None:
    if moov.offset < mdat.offset or moov.size > MAX_MOOV_BYTES or moov.size < 16:
        return None
    if len(moov_bytes) != moov.size or moov_bytes[4:8] != b"moov":
        return None
    return FaststartLayout(
        file_size=file_size,
        prefix_end=mdat.offset,
        moov_phys=moov.offset,
        moov_size=moov.size,
        gap_phys=mdat.offset,
        gap_size=moov.offset - mdat.offset,
        tail_phys=moov.offset + moov.size,
        tail_size=file_size - (moov.offset + moov.size),
        patched_moov=patch_chunk_offsets(moov_bytes, moov.size),
    )


def plan_faststart(file_size: int, read) -> FaststartLayout | None:
    if file_size < 16:
        return None
    atoms = walk_top_atoms(file_size, read)
    moov = next((atom for atom in atoms if atom.typ == b"moov"), None)
    mdat = next((atom for atom in atoms if atom.typ == b"mdat"), None)
    if moov is None:
        window = min(file_size, 1 * 1024 * 1024)
        while window <= min(file_size, MAX_MOOV_BYTES):
            tail_offset = file_size - window
            moov = moov_ending_at_eof(read(tail_offset, window), tail_offset, file_size)
            if moov is not None or window >= file_size:
                break
            window = min(file_size, window * 4)
    if moov is None or mdat is None:
        return None
    return layout_from_moov(file_size, mdat, moov, read(moov.offset, moov.size))


def materialize(layout: FaststartLayout, read) -> bytes:
    pieces: list[bytes] = []
    for kind, offset, length in layout.segments(0, layout.file_size - 1):
        if kind == "moov":
            pieces.append(layout.patched_moov[offset:offset + length])
        else:
            pieces.append(read(offset, length))
    return b"".join(pieces)
