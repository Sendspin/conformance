"""
`sendspin-validate` finds no fault in the bundled aiosendspin client.

Runs the real console script against a known-good client in both connection
directions over localhost, through the opening checks and every suite, so the
machine checks are proven to give no false FAIL or TOOL_ERROR. A scripted operator
plays the user, doing in the client what each step's instruction asks. Runs against
a client that skips the handshake, and one that ignores server/unpair, show a fault
fails the run. mDNS is not used: multicast is unreliable on CI runners.
"""

import asyncio
import json
import os
import secrets
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import ClientError, ClientSession, WSMsgType

from conformance._vendor.aiosendspin.client import ClientListener, SendspinClient
from conformance._vendor.aiosendspin.client.connection import SendspinConnection
from conformance._vendor.aiosendspin.models.player import (
    ClientHelloPlayerSupport,
    SupportedAudioFormat,
)
from conformance._vendor.aiosendspin.models.types import (
    AudioCodec,
    GoodbyeReason,
    PlayerCommand,
    Roles,
)
from conformance._vendor.aiosendspin.noise import (
    Identity,
    InMemoryClientPairingStore,
    PairingPsk,
    PSKPairingToken,
    b64url_encode,
    encode_psk_token,
    psk_id_for,
)
from conformance.validator.report import bundled_aiosendspin

VALIDATOR = Path(sys.executable).with_name("sendspin-validate")
RUN_TIMEOUT_S = 240.0
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
# Suite checks the bundled client must pass; U4 is opt-in and runs in its own test.
SUITE_CHECKS = {
    "UNPAIRED-001",
    "UNPAIRED-002",
    "UNPAIRED-003",
    *(f"PAIR-PSK-00{n}" for n in range(1, 8)),
    *(f"POSTPAIR-00{n}" for n in range(1, 7)),
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def known_good_client(identity: Identity, pairing_psk: bytes) -> SendspinClient:
    store = InMemoryClientPairingStore()
    await store.set_pairing_psk(PairingPsk(psk_id=psk_id_for(pairing_psk), psk=pairing_psk))
    client = SendspinClient(
        identity=identity,
        client_name="Validator Self-Test",
        roles=[Roles.PLAYER, Roles.CONTROLLER, Roles.METADATA],
        pairing_store=store,
        player_support=ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(
                    codec=AudioCodec.PCM, channels=2, sample_rate=48_000, bit_depth=16
                )
            ],
            buffer_capacity=2_000_000,
        ),
        state_supported_commands=[PlayerCommand.SET_OUTPUT_DELAY],
    )
    await client.set_unpaired_access(enabled=True)
    return client


class Operator:
    """
    Plays the person running the validator, using the bundled client as their app.

    It reads the validator's output and does in the client what each step asks. In
    client-initiated mode it also keeps the app connected, as a real app reconnects
    on its own. It keeps every long-term PSK the client was given, so a test can
    check none of them leaked.
    """

    def __init__(self, client: SendspinClient, url: str | None) -> None:
        self.client = client
        self.long_term_psks: set[str] = set()
        self.transcript: list[str] = []
        self._url = url

    async def follow(self, output: asyncio.StreamReader) -> None:
        """Act on each line of the validator's output until it ends."""
        reconnecting = asyncio.create_task(self._stay_connected()) if self._url else None
        try:
            while line := (await output.readline()).decode():
                self.transcript.append(line)
                await self._act(line)
        finally:
            if reconnecting is not None:
                reconnecting.cancel()

    async def _act(self, line: str) -> None:
        if line.startswith(("[U2]", "[U4]")):
            await self.client.set_unpaired_access(enabled=False)
        elif line.startswith("[U3]"):
            await self.client.set_unpaired_access(enabled=True)
        elif line.startswith(("[R1]", "[R4]")):
            for record in await self.client.pairing_store.list_records():
                self.long_term_psks.add(b64url_encode(record.psk))
        elif line.startswith("Now fully quit your app"):
            await self.client.disconnect(GoodbyeReason.RESTART)

    async def _stay_connected(self) -> None:
        assert self._url is not None
        while True:
            if not self.client.connected:
                try:
                    await self.client.connect(self._url)
                except (ClientError, OSError, RuntimeError):
                    pass
            await asyncio.sleep(0.2)


class ValidatorSelfTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        self.workdir = Path(workdir.name)
        self.identity = Identity.generate()
        self.pairing_psk = secrets.token_bytes(32)
        self.token = encode_psk_token(
            PSKPairingToken(client_id=self.identity.peer_id, pairing_psk=self.pairing_psk)
        )
        self.client = await known_good_client(self.identity, self.pairing_psk)
        self.addAsyncCleanup(self.client.disconnect)

    async def test_server_initiated(self) -> None:
        url = await self._listen()
        report = await self._finish(await self._run(None, "--connect", url))
        self.assertEqual(report["connection_mode"], "server-initiated")

    async def test_client_initiated(self) -> None:
        port = free_port()
        report = await self._finish(
            await self._run(f"ws://127.0.0.1:{port}/sendspin", "--port", str(port))
        )
        self.assertEqual(report["connection_mode"], "client-initiated")

    async def test_inadmissible_activation(self) -> None:
        """
        U4 fails the bundled client, which answers with the wrong goodbye reason.

        It decides messaging.md:253's rule 1 on the activity set and roles alone, so it
        says 'pairing_required'. rc1 counts the pairing method as part of being
        admissible (messaging.md:247, :251), which no unpaired access fixes here, so
        rule 2's 'unauthorized' applies. The test pins that verdict until either side
        changes.
        """
        url = await self._listen()
        report = await self._report(
            await self._run(None, "--connect", url, "--suite", "unpaired", "--fault-injection")
        )
        self.assertEqual(self.returncode, 1, self.transcript)
        verdicts = {f["id"]: f["verdict"] for f in report["findings"]}
        for check in ("UNPAIRED-001", "UNPAIRED-002", "UNPAIRED-003"):
            self.assertEqual(verdicts[check], "PASS", self.transcript)
        self.assertEqual(verdicts["UNPAIRED-004"], "FAIL", self.transcript)
        u4 = next(f for f in report["findings"] if f["id"] == "UNPAIRED-004")
        self.assertIn("'pairing_required', not 'unauthorized'", u4["message"])

    async def test_client_that_ignores_unpair_fails(self) -> None:
        url = await self._listen()

        async def ignore(_connection: SendspinConnection) -> None:
            return None

        with mock.patch.object(SendspinConnection, "_handle_unpair", ignore):
            report = await self._report(
                await self._run(None, "--connect", url, "--suite", "pairing,post-pairing")
            )
        self.assertEqual(self.returncode, 1, self.transcript)
        verdicts = {f["id"]: f["verdict"] for f in report["findings"]}
        self.assertEqual(verdicts["PAIR-PSK-007"], "PASS", self.transcript)
        self.assertEqual(verdicts["POSTPAIR-003"], "FAIL", self.transcript)
        self.assertEqual(verdicts["POSTPAIR-004"], "FAIL", self.transcript)

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
            output, _ = await validator.communicate()
        self.transcript = output.decode()
        self.returncode = validator.returncode
        report = self._read_report()

        self.assertEqual(self.returncode, 1)
        verdicts = {f["id"]: f["verdict"] for f in report["findings"]}
        self.assertEqual(verdicts["CORE-INIT-001"], "FAIL")
        self.assertEqual(verdicts["CORE-INIT-002"], "N/A")

    async def _listen(self) -> str:
        """Listen for the validator's dial, as an app does in server-initiated mode."""
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
        return f"ws://127.0.0.1:{port}/sendspin"

    async def _run(self, url: str | None, *args: str) -> Operator:
        """Run the validator with `args` while an operator drives the client through it."""
        validator = await self._start_validator(
            "--verbose", "--auto-answer-token", self.token, *args
        )
        operator = Operator(self.client, url)
        assert validator.stdout is not None
        async with asyncio.timeout(RUN_TIMEOUT_S):
            await operator.follow(validator.stdout)
            await validator.wait()
        self.transcript = "".join(operator.transcript)
        self.returncode = validator.returncode
        return operator

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

    async def _report(self, operator: Operator) -> dict:
        """Check the run wrote all three files and leaked no secret, then return its report."""
        report = self._read_report()
        run = next((self.workdir / "runs").glob("sendspin-validate-*"))
        secrets_seen = {
            self.token,
            self.token.removeprefix("SP:"),
            b64url_encode(self.pairing_psk),
            *operator.long_term_psks,
        }
        outputs = {
            "--verbose output": self.transcript,
            **{name: (run / name).read_text() for name in ("report.json", "report.html")},
            "protocol.log.jsonl": (run / "protocol.log.jsonl").read_text(),
        }
        for name, text in outputs.items():
            for secret in secrets_seen:
                self.assertNotIn(secret, text, f"a pairing secret leaked into {name}")
        return report

    def _read_report(self) -> dict:
        runs = list((self.workdir / "runs").glob("sendspin-validate-*"))
        self.assertEqual(len(runs), 1, self.transcript)
        run = runs[0]
        for name in ("report.json", "report.html", "protocol.log.jsonl"):
            self.assertTrue((run / name).is_file(), f"{name} missing\n{self.transcript}")
        return json.loads((run / "report.json").read_text())

    async def _finish(self, operator: Operator, *, expected: set[str] = SUITE_CHECKS) -> dict:
        """Return the run's report after checking it is clean and passed `expected`."""
        report = await self._report(operator)
        transcript = self.transcript
        if any(check.startswith("POSTPAIR-") for check in expected):
            self.assertTrue(operator.long_term_psks, transcript)
        faults = [f for f in report["findings"] if f["verdict"] in {"FAIL", "TOOL_ERROR"}]
        self.assertEqual(faults, [], transcript)
        self.assertEqual(self.returncode, 0, transcript)
        passed = {f["id"] for f in report["findings"] if f["verdict"] == "PASS"}
        self.assertLessEqual(OPENING_CHECKS | expected, passed, transcript)
        self.assertEqual(report["client"]["name"], "Validator Self-Test")
        self.assertEqual(report["client"]["client_id"], self.identity.peer_id)
        self.assertEqual(report["spec"], "1.0.0-rc1")
        self.assertEqual(report["aiosendspin"], bundled_aiosendspin())
        return report


if __name__ == "__main__":
    unittest.main()
