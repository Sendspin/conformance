"""
Coverage for the aiosendspin server adapter waiting on client availability.

The audio scenarios commit their whole clip before any of it plays, so a stream
started while the client still reports unavailable loses its head. The matrix
only shows that as a hash mismatch; the wait itself is exercised here against a
stand-in for the SDK client.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from conformance.adapters.aiosendspin_server import _wait_for_client_available


class WaitForClientAvailableTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_at_once_for_an_available_client(self) -> None:
        client = SimpleNamespace(name="client", available=True)

        await asyncio.wait_for(_wait_for_client_available(client, timeout_s=5.0), timeout=1.0)

    async def test_waits_until_the_client_reports_available(self) -> None:
        client = SimpleNamespace(name="client", available=False)
        waiter = asyncio.create_task(_wait_for_client_available(client, timeout_s=5.0))

        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done())

        client.available = True
        await asyncio.wait_for(waiter, timeout=1.0)

    async def test_times_out_when_the_client_never_reports_available(self) -> None:
        client = SimpleNamespace(name="client", available=False)

        with self.assertRaisesRegex(TimeoutError, "'client' to report available"):
            await _wait_for_client_available(client, timeout_s=0.2)


if __name__ == "__main__":
    unittest.main()
