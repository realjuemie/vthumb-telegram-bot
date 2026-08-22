import tempfile
import unittest
from pathlib import Path

from app.access_control import AccessControl


class AccessControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_is_always_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            access = AccessControl(Path(directory) / "access.json", frozenset({123456789}))
            self.assertTrue(access.is_admin(123456789))
            self.assertTrue(access.is_allowed(123456789))

    async def test_users_persist_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            access = AccessControl(path, frozenset({123456789}))
            self.assertTrue(await access.add(987654321))

            reloaded = AccessControl(path, frozenset({123456789}))
            self.assertTrue(reloaded.is_allowed(987654321))
            self.assertEqual(reloaded.users(), [987654321])

    async def test_remove_revokes_user_but_not_admin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            access = AccessControl(Path(directory) / "access.json", frozenset({123456789}))
            await access.add(987654321)
            self.assertTrue(await access.remove(987654321))
            self.assertFalse(access.is_allowed(987654321))
            self.assertFalse(await access.remove(123456789))
            self.assertTrue(access.is_allowed(123456789))

    async def test_open_access_allows_anyone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            access = AccessControl(
                Path(directory) / "access.json",
                frozenset({123456789}),
                open_access=True,
            )
            self.assertTrue(access.is_allowed(111))
            self.assertTrue(access.is_allowed(123456789))
            self.assertFalse(access.is_allowed(None))


if __name__ == "__main__":
    unittest.main()
