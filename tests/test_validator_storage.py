"""The validator keeps its identity across runs and admits every client unpaired."""

import stat
import tempfile
import unittest
from pathlib import Path

from conformance.validator.storage import load_state


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "data"

    async def test_identity_persists_until_fresh(self) -> None:
        first, _ = await load_state(self.directory, fresh=False)
        again, _ = await load_state(self.directory, fresh=False)
        fresh, _ = await load_state(self.directory, fresh=True)
        self.assertEqual(again.peer_id, first.peer_id)
        self.assertNotEqual(fresh.peer_id, first.peer_id)
        mode = (self.directory / "identity.json").stat().st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    async def test_every_client_is_trusted_unpaired(self) -> None:
        _, store = await load_state(self.directory, fresh=False)
        self.assertIsNotNone(await store.trusted_unpaired("any-client"))
        self.assertFalse(await store.list_trusted_unpaired())

    async def test_unpaired_admission_can_be_withheld(self) -> None:
        _, store = await load_state(self.directory, fresh=False)
        store.admit_unpaired = False
        self.assertIsNone(await store.trusted_unpaired("any-client"))


if __name__ == "__main__":
    unittest.main()
