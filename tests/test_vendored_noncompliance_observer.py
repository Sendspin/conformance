"""The bundled aiosendspin server reports every spec violation to its non-compliance observer."""

import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from conformance._vendor.aiosendspin.client import SendspinClient
from conformance._vendor.aiosendspin.models.types import Roles
from conformance._vendor.aiosendspin.noise import (
    Identity,
    InMemoryClientPairingStore,
    InMemoryServerPairingStore,
)
from conformance._vendor.aiosendspin.server import (
    ObservedFrame,
    ObservedNoncompliance,
    SendspinServer,
)

TIMEOUT_S = 10.0


class NoncomplianceObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_server_reports_the_violation_it_rejects(self) -> None:
        frames: list[ObservedFrame] = []
        observed: list[ObservedNoncompliance] = []
        flagged = asyncio.Event()

        def observe(observation: ObservedNoncompliance) -> None:
            observed.append(observation)
            flagged.set()

        server = SendspinServer(
            loop=asyncio.get_running_loop(),
            identity=Identity.generate(),
            server_name="noncompliance-observer-server",
            pairing_store=InMemoryServerPairingStore(),
            allow_noncompliant_clients=False,
            frame_observer=frames.append,
            noncompliance_observer=observe,
        )
        self.addAsyncCleanup(server.close)
        app = web.Application()
        app.router.add_get(SendspinServer.API_PATH, server.on_client_connect)
        test_server = TestServer(app, host="127.0.0.1")
        await test_server.start_server()
        self.addAsyncCleanup(test_server.close)
        client_identity = Identity.generate()
        client = SendspinClient(
            identity=client_identity,
            client_name="noncompliance-observer-client",
            roles=[Roles.CONTROLLER],
            pairing_store=InMemoryClientPairingStore(),
        )
        self.addAsyncCleanup(client.disconnect)

        await client.connect(f"ws://127.0.0.1:{test_server.port}{SendspinServer.API_PATH}")
        # client/state without the required `available` field.
        await client._admitted_connection._ws.send_str(  # noqa: SLF001
            json.dumps({"type": "client/state", "payload": {}})
        )
        async with asyncio.timeout(TIMEOUT_S):
            await flagged.wait()

        self.assertEqual(
            [(o.connection, o.client_id, o.reason) for o in observed],
            [
                (
                    frames[0].connection,
                    client_identity.peer_id,
                    "client/state omitted the required 'available' field",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
