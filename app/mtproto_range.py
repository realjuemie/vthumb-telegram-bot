import asyncio
import logging
import os
import secrets
import shutil
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from .mp4_index import FaststartLayout, layout_from_moov, moov_ending_at_eof, parse_atom_header


class FetchBudgetExceeded(RuntimeError):
    def __init__(self, fetched: int, budget: int, size: int) -> None:
        self.fetched = fetched
        self.budget = budget
        self.size = size
        super().__init__(
            f"Source read hard limit reached "
            f"({fetched // 1024 // 1024}MB read, {budget // 1024 // 1024}MB limit)."
        )


class InvalidRange(ValueError):
    pass


@dataclass
class MediaRegistration:
    media: Any
    size: int
    filename: str
    budget: int
    hard_budget: int | None = None
    budget_growth: int = 16 * 1024 * 1024
    fetched: int = 0
    cache_hits: int = 0
    disk_hits: int = 0
    failure: Exception | None = None
    cache_dir: Path | None = None
    layout: FaststartLayout | None = None
    layout_checked: bool = False

    def __post_init__(self) -> None:
        if self.hard_budget is None:
            self.hard_budget = self.budget
        self.hard_budget = max(self.budget, min(self.size, self.hard_budget))


def parse_byte_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if size <= 0:
        raise InvalidRange("File size is unknown.")
    if not value:
        return 0, size - 1, False
    if not value.startswith("bytes=") or "," in value:
        raise InvalidRange("Only a single byte range is supported.")

    spec = value[6:].strip()
    if "-" not in spec:
        raise InvalidRange("Invalid byte range.")
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise InvalidRange("Invalid suffix range.")
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError as exc:
        raise InvalidRange("Invalid byte range.") from exc

    if start < 0 or start >= size or end < start:
        raise InvalidRange("Requested range is outside the file.")
    return start, min(end, size - 1), True


OPEN_ENDED_CAP = 8 * 1024 * 1024


def cap_open_ended(value: str | None, start: int, end: int) -> tuple[int, int, bool]:
    """Don't stream a whole file when the client leaves the range end open."""
    if not value or not value.startswith("bytes=") or "," in value:
        return start, end, False
    spec = value[6:].strip()
    if "-" not in spec:
        return start, end, False
    start_text, end_text = spec.split("-", 1)
    if not start_text or end_text:
        return start, end, False
    if end - start + 1 <= OPEN_ENDED_CAP:
        return start, end, True
    return start, start + OPEN_ENDED_CAP - 1, True


class MTProtoRangeServer:
    def __init__(
        self,
        client: Any,
        *,
        port: int = 8090,
        chunk_size: int = 512 * 1024,
        cache_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        self.client = client
        self.port = port
        self.chunk_size = chunk_size
        self.cache_bytes = cache_bytes
        self.registrations: dict[str, MediaRegistration] = {}
        self.source_locks: dict[str, asyncio.Lock] = {}
        self.download_slots = asyncio.Semaphore(3)
        self.budget_lock = asyncio.Lock()
        self.layout_locks: dict[str, asyncio.Lock] = {}
        self.chunk_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self.cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self.cache_size = 0
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("GET", "/media/{key}", self._handle)
        app.router.add_route("HEAD", "/media/{key}", self._handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()

    async def stop(self) -> None:
        for key in list(self.registrations):
            self.unregister(key)
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    def register(
        self,
        media: Any,
        size: int,
        filename: str,
        budget: int,
        *,
        hard_budget: int | None = None,
        budget_growth: int = 16 * 1024 * 1024,
    ) -> tuple[str, str]:
        if media is None:
            raise RuntimeError("Telegram message does not contain downloadable media.")
        key = secrets.token_urlsafe(18)
        cache_dir = Path(tempfile.mkdtemp(prefix=f"vthumb_range_{key}_"))
        self.registrations[key] = MediaRegistration(
            media,
            size,
            filename,
            budget,
            hard_budget=hard_budget,
            budget_growth=budget_growth,
            cache_dir=cache_dir,
        )
        self.source_locks[key] = asyncio.Lock()
        self.layout_locks[key] = asyncio.Lock()
        return key, f"http://127.0.0.1:{self.port}/media/{key}"

    def failure(self, key: str) -> Exception | None:
        registration = self.registrations.get(key)
        return registration.failure if registration else None

    def unregister(self, key: str) -> None:
        registration = self.registrations.pop(key, None)
        self.source_locks.pop(key, None)
        self.layout_locks.pop(key, None)
        for chunk_key in [item for item in self.chunk_locks if item[0] == key]:
            self.chunk_locks.pop(chunk_key, None)
        if registration:
            logging.info(
                "MTProto source finished: file=%r fetched=%d bytes memory_hits=%d "
                "disk_hits=%d budget=%d hard_budget=%d bytes",
                registration.filename,
                registration.fetched,
                registration.cache_hits,
                registration.disk_hits,
                registration.budget,
                registration.hard_budget,
            )
            if registration.cache_dir:
                shutil.rmtree(registration.cache_dir, ignore_errors=True)
        for cache_key in [item for item in self.cache if item[0] == key]:
            self.cache_size -= len(self.cache.pop(cache_key))

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        key = request.match_info["key"]
        registration = self.registrations.get(key)
        if not registration:
            raise web.HTTPNotFound()
        try:
            range_header = request.headers.get("Range")
            start, end, partial = parse_byte_range(range_header, registration.size)
            capped_start, capped_end, capped = cap_open_ended(range_header, start, end)
            if capped and capped_end != end:
                start, end, partial = capped_start, capped_end, True
        except InvalidRange:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{registration.size}"})

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(end - start + 1),
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{registration.size}"
        response = web.StreamResponse(status=206 if partial else 200, headers=headers)
        await response.prepare(request)
        if request.method == "HEAD":
            return response

        try:
            layout = await self._ensure_layout(key, registration)
            if layout is None:
                await self._write_span(response, key, registration, start, end - start + 1)
            else:
                for kind, offset, length in layout.segments(start, end):
                    if kind == "moov":
                        await response.write(layout.patched_moov[offset:offset + length])
                    else:
                        await self._write_span(response, key, registration, offset, length)
            await response.write_eof()
        except ConnectionError:
            pass
        except Exception as exc:
            registration.failure = exc
            logging.warning("MTProto range request stopped (%s)", type(exc).__name__)
        return response

    async def _ensure_layout(self, key: str, registration: MediaRegistration) -> FaststartLayout | None:
        if registration.layout_checked:
            return registration.layout
        lock = self.layout_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if registration.layout_checked:
                return registration.layout
            try:
                registration.layout = await self._plan_layout(key, registration)
            except FetchBudgetExceeded:
                raise
            except Exception as exc:
                logging.warning("MP4 index scan failed (%s)", type(exc).__name__)
                registration.layout = None
            registration.layout_checked = True
            if registration.layout is not None:
                logging.info(
                    "MP4 index is at the end; serving a faststart view file=%r moov=%d shift=%d",
                    registration.filename,
                    registration.layout.moov_size,
                    registration.layout.moov_size,
                )
            return registration.layout

    async def _plan_layout(self, key: str, registration: MediaRegistration) -> FaststartLayout | None:
        async def read(offset: int, length: int) -> bytes:
            return await self._read_span(key, registration, offset, length)

        file_size = registration.size
        atoms = []
        offset = 0
        for _ in range(64):
            if offset + 8 > file_size:
                break
            atom = parse_atom_header(await read(offset, 16), file_size, offset)
            if atom is None:
                break
            atoms.append(atom)
            if atom.typ == b"moov" and any(item.typ == b"mdat" for item in atoms):
                break
            if atom.offset + atom.size >= file_size:
                break
            offset += atom.size
        moov = next((atom for atom in atoms if atom.typ == b"moov"), None)
        mdat = next((atom for atom in atoms if atom.typ == b"mdat"), None)
        if moov is None and mdat is not None:
            window = min(file_size, 1024 * 1024)
            while window <= min(file_size, 48 * 1024 * 1024):
                tail_offset = file_size - window
                moov = moov_ending_at_eof(await read(tail_offset, window), tail_offset, file_size)
                if moov is not None or window >= file_size:
                    break
                window = min(file_size, window * 4)
        if moov is None or mdat is None or moov.offset < mdat.offset:
            return None
        if moov.size > 48 * 1024 * 1024:
            return None
        return layout_from_moov(file_size, mdat, moov, await read(moov.offset, moov.size))

    async def _read_span(self, key: str, registration: MediaRegistration, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        end = min(registration.size, offset + length)
        pieces: list[bytes] = []
        cursor = offset
        while cursor < end:
            chunk_index = cursor // self.chunk_size
            chunk = await self._get_chunk(key, registration, chunk_index)
            chunk_start = chunk_index * self.chunk_size
            left = cursor - chunk_start
            take = min(len(chunk) - left, end - cursor)
            if take <= 0:
                break
            pieces.append(chunk[left:left + take])
            cursor += take
        return b"".join(pieces)

    async def _write_span(self, response: web.StreamResponse, key: str, registration: MediaRegistration, offset: int, length: int) -> None:
        end = offset + length
        cursor = offset
        while cursor < end:
            chunk_index = cursor // self.chunk_size
            chunk = await self._get_chunk(key, registration, chunk_index)
            chunk_start = chunk_index * self.chunk_size
            left = max(cursor, chunk_start) - chunk_start
            right = min(end, chunk_start + len(chunk)) - chunk_start
            if right > left:
                await response.write(chunk[left:right])
            cursor = chunk_start + len(chunk)
            if cursor <= chunk_start:
                break

    async def _get_chunk(
        self,
        key: str,
        registration: MediaRegistration,
        chunk_index: int,
    ) -> bytes:
        cache_key = (key, chunk_index)
        cached = self._take_cached(cache_key, registration)
        if cached is not None:
            return cached
        cached = self._take_disk_cached(cache_key, registration)
        if cached is not None:
            return cached

        lock = self.chunk_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            async with self.download_slots:
                if registration.failure:
                    raise registration.failure
                cached = self._take_cached(cache_key, registration)
                if cached is not None:
                    return cached
                cached = self._take_disk_cached(cache_key, registration)
                if cached is not None:
                    return cached

                offset = chunk_index * self.chunk_size
                expected = min(self.chunk_size, registration.size - offset)
                async with self.budget_lock:
                    self._ensure_budget(registration, expected)

                iterator = self.client.iter_download(
                    registration.media,
                    offset=offset,
                    limit=1,
                    chunk_size=self.chunk_size,
                    request_size=self.chunk_size,
                    file_size=registration.size,
                )
                chunk = b""
                async for item in iterator:
                    chunk = bytes(item)
                    break
                if not chunk:
                    raise RuntimeError("Telegram returned an empty media chunk.")

                async with self.budget_lock:
                    registration.fetched += len(chunk)
                self._store_disk_chunk(registration, chunk_index, chunk)
                self._store_memory_chunk(cache_key, chunk)
                return chunk

    @staticmethod
    def _ensure_budget(registration: MediaRegistration, expected: int) -> None:
        needed = registration.fetched + expected
        if needed <= registration.budget:
            return

        hard_budget = registration.hard_budget or registration.budget
        if needed > hard_budget:
            raise FetchBudgetExceeded(registration.fetched, hard_budget, registration.size)

        old_budget = registration.budget
        growth = max(expected, registration.budget_growth)
        registration.budget = min(hard_budget, max(needed, old_budget + growth))
        logging.info(
            "MTProto source budget expanded: file=%r old=%d new=%d hard=%d bytes",
            registration.filename,
            old_budget,
            registration.budget,
            hard_budget,
        )

    def _take_disk_cached(
        self,
        cache_key: tuple[str, int],
        registration: MediaRegistration,
    ) -> bytes | None:
        if not registration.cache_dir:
            return None
        path = registration.cache_dir / f"{cache_key[1]}.chunk"
        try:
            chunk = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logging.warning("Could not read range cache chunk (%s)", type(exc).__name__)
            return None
        registration.disk_hits += 1
        self._store_memory_chunk(cache_key, chunk)
        return chunk

    def _store_disk_chunk(
        self,
        registration: MediaRegistration,
        chunk_index: int,
        chunk: bytes,
    ) -> None:
        if not registration.cache_dir:
            return
        path = registration.cache_dir / f"{chunk_index}.chunk"
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_bytes(chunk)
            os.replace(temporary, path)
        except OSError as exc:
            logging.warning("Could not persist range cache chunk (%s)", type(exc).__name__)
            temporary.unlink(missing_ok=True)

    def _store_memory_chunk(self, cache_key: tuple[str, int], chunk: bytes) -> None:
        previous = self.cache.pop(cache_key, None)
        if previous is not None:
            self.cache_size -= len(previous)
        self.cache[cache_key] = chunk
        self.cache_size += len(chunk)
        while self.cache_size > self.cache_bytes and self.cache:
            _, removed = self.cache.popitem(last=False)
            self.cache_size -= len(removed)

    def _take_cached(
        self,
        cache_key: tuple[str, int],
        registration: MediaRegistration,
    ) -> bytes | None:
        cached = self.cache.pop(cache_key, None)
        if cached is not None:
            self.cache[cache_key] = cached
            registration.cache_hits += 1
        return cached
