"""
`sendspin-validate` finds no fault in the bundled aiosendspin client.

Runs the real console script against a known-good client in both connection
directions over localhost, so the machine checks are proven to give no false FAIL
or TOOL_ERROR, and against a client that skips the handshake, so a fault is shown
to fail the run. mDNS is not used: multicast is unreliable on CI runners.
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientError, ClientSession, WSMsgType

from conformance._vendor.aiosendspin.client import ClientListener, SendspinClient
from conformance._vendor.aiosendspin.models.player import (
    ClientHelloPlayerSupport,
    SupportedAudioFormat,
)
from conformance._vendor.aiosendspin.models.types import AudioCodec, Roles
from conformance._vendor.aiosendspin.noise import Identity, InMemoryClientPairingStore
from conformance.validator.report import bundled_aiosendspin

VALIDATOR = Path(sys.executable).with_name("sendspin-validate")
RUN_TIMEOUT_S = 90.0
# Checks a session that observed the whole opening exchange must pass.
OPENING_CHECKS = {
    "CORE-INIT-001",
    "CORE-NOISE-001",
    "CORE-ORDER-001",
    "CORE-HELLO-001",
    "CORE-STATE-001",
    "CORE-TIME-001",
    "CORE-STRICT-001",
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def known_good_client(identity: Identity) -> SendspinClient:
    client = SendspinClient(
        identity=identity,
        client_name="Validator Self-Test",
        roles=[Roles.PLAYER, Roles.CONTROLLER, Roles.METADATA],
        pairing_store=InMemoryClientPairingStore(),
        player_support=ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(
                    codec=AudioCodec.PCM, channels=2, sample_rate=48_000, bit_depth=16
                )
            ],
            buffer_capacity=2_000_000,
        ),
    )
    await client.set_unpaired_access(enabled=True)
    return client


class ValidatorSelfTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        self.workdir = Path(workdir.name)
        self.identity = Identity.generate()
        self.client = await known_good_client(self.identity)
        self.addAsyncCleanup(self.client.disconnect)

    async def test_server_initiated(self) -> None:
        port = free_port()
        listener = ClientListener(
            client_id=self.identity.peer_id,
            on_connection=self.client.attach_websocket,
            port=port,
            host="127.0.0.1",
            advertise_mdns=False,
        )
        await listener.start()
        self.addAsyncCleanup(listener.stop)

        validator = await self._start_validator("--connect", f"ws://127.0.0.1:{port}/sendspin")

        report = await self._finish(validator)
        self.assertEqual(report["connection_mode"], "server-initiated")

    async def test_client_initiated(self) -> None:
        port = free_port()
        validator = await self._start_validator("--port", str(port))

        async with asyncio.timeout(RUN_TIMEOUT_S):
            while True:
                try:
                    await self.client.connect(f"ws://127.0.0.1:{port}/sendspin")
                    break
                except (ClientError, OSError):
                    await asyncio.sleep(0.2)

        report = await self._finish(validator)
        self.assertEqual(report["connection_mode"], "client-initiated")

    async def test_client_without_a_handshake_fails(self) -> None:
        port = free_port()
        validator = await self._start_validator("--port", str(port))
        hello = {"type": "client/hello", "payload": {"name": "Legacy", "supported_roles": []}}

        async with asyncio.timeout(RUN_TIMEOUT_S), ClientSession() as session:
            while True:
                try:
                    ws = await session.ws_connect(f"ws://127.0.0.1:{port}/sendspin")
                    break
                except (ClientError, OSError):
                    await asyncio.sleep(0.2)
            await ws.send_str(json.dumps(hello))
            while (await ws.receive()).type is WSMsgType.TEXT:
                pass
            report = await self._report(validator)

        self.assertEqual(validator.returncode, 1)
        verdicts = {f["id"]: f["verdict"] for f in report["findings"]}
        self.assertEqual(verdicts["CORE-INIT-001"], "FAIL")
        self.assertEqual(verdicts["CORE-INIT-002"], "N/A")

    async def _start_validator(self, *args: str) -> asyncio.subprocess.Process:
        env = {**os.environ, "HOME": str(self.workdir), "XDG_DATA_HOME": str(self.workdir)}
        return await asyncio.create_subprocess_exec(
            str(VALIDATOR),
            "--auto-answer",
            "--timeout",
            "30",
            "--output-dir",
            str(self.workdir / "runs"),
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    async def _report(self, validator: asyncio.subprocess.Process) -> dict:
        """Wait for the validator, check it wrote all three files, and return its report."""
        async with asyncio.timeout(RUN_TIMEOUT_S):
            output, _ = await validator.communicate()
        self.transcript = output.decode()
        runs = list((self.workdir / "runs").glob("sendspin-validate-*"))
        self.assertEqual(len(runs), 1, self.transcript)
        run = runs[0]
        for name in ("report.json", "report.html", "protocol.log.jsonl"):
            self.assertTrue((run / name).is_file(), f"{name} missing\n{self.transcript}")
        return json.loads((run / "report.json").read_text())

    async def _finish(self, validator: asyncio.subprocess.Process) -> dict:
        """Wait for the validator, then return its report after checking it is clean."""
        report = await self._report(validator)
        transcript = self.transcript
        faults = [f for f in report["findings"] if f["verdict"] in {"FAIL", "TOOL_ERROR"}]
        self.assertEqual(faults, [], transcript)
        self.assertEqual(validator.returncode, 0, transcript)
        passed = {f["id"] for f in report["findings"] if f["verdict"] == "PASS"}
        self.assertLessEqual(OPENING_CHECKS, passed, transcript)
        self.assertEqual(report["client"]["name"], "Validator Self-Test")
        self.assertEqual(report["client"]["client_id"], self.identity.peer_id)
        self.assertEqual(report["spec"], "1.0.0-rc1")
        self.assertEqual(report["aiosendspin"], bundled_aiosendspin())
        return report


if __name__ == "__main__":
    unittest.main()
