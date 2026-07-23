import asyncio
import unittest

from app.mtproto_range import (
    FetchBudgetExceeded,
    InvalidRange,
    MTProtoRangeServer,
    MediaRegistration,
    parse_byte_range,
)


class FakeTelegramClient:
    def __init__(self) -> None:
        self.calls = 0

    def iter_download(self, _media, *, offset, chunk_size, file_size, **_kwargs):
        self.calls += 1

        async def chunks():
            await asyncio.sleep(0.01)
            yield bytes([offset // chunk_size]) * min(chunk_size, file_size - offset)

        return chunks()


class ByteRangeTests(unittest.TestCase):
    def test_open_ended_range(self) -> None:
        self.assertEqual(parse_byte_range("bytes=512-", 1024), (512, 1023, True))

    def test_suffix_range(self) -> None:
        self.assertEqual(parse_byte_range("bytes=-100", 1024), (924, 1023, True))

    def test_outside_range_is_rejected(self) -> None:
        with self.assertRaises(InvalidRange):
            parse_byte_range("bytes=1024-", 1024)


class ChunkCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_media_registration(self) -> None:
        media = object()
        server = MTProtoRangeServer(FakeTelegramClient(), chunk_size=8, cache_bytes=16)

        key, url = server.register(media, 24, "test.mp4", budget=8)

        self.assertIs(server.registrations[key].media, media)
        self.assertTrue(url.endswith(key))
        server.unregister(key)

    async def test_chunk_is_reused_from_cache(self) -> None:
        client = FakeTelegramClient()
        server = MTProtoRangeServer(client, chunk_size=8, cache_bytes=16)
        registration = MediaRegistration(object(), 24, "test.mp4", budget=16)

        first = await server._get_chunk("source", registration, 0)
        second = await server._get_chunk("source", registration, 0)

        self.assertEqual(first, second)
        self.assertEqual(client.calls, 1)
        self.assertEqual(registration.fetched, 8)
        self.assertEqual(registration.cache_hits, 1)

    async def test_concurrent_requests_share_one_download(self) -> None:
        client = FakeTelegramClient()
        server = MTProtoRangeServer(client, chunk_size=8, cache_bytes=16)
        registration = MediaRegistration(object(), 24, "test.mp4", budget=8)

        first, second = await asyncio.gather(
            server._get_chunk("source", registration, 0),
            server._get_chunk("source", registration, 0),
        )

        self.assertEqual(first, second)
        self.assertEqual(client.calls, 1)
        self.assertEqual(registration.fetched, 8)
        self.assertEqual(registration.cache_hits, 1)

    async def test_budget_stops_additional_downloads(self) -> None:
        client = FakeTelegramClient()
        server = MTProtoRangeServer(client, chunk_size=8, cache_bytes=16)
        registration = MediaRegistration(object(), 24, "test.mp4", budget=8)

        await server._get_chunk("source", registration, 0)
        with self.assertRaises(FetchBudgetExceeded):
            await server._get_chunk("source", registration, 1)
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
