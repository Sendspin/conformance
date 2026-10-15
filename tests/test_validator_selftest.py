"""
`sendspin-validate` finds no fault in the bundled aiosendspin client.

Runs the real console script against a known-good client in both connection
directions over localhost, through the opening checks and every suite, so the
machine checks are proven to give no false FAIL or TOOL_ERROR. The client offers the
dynamic pairing code in both formats, or in its own runs the static one. A scripted
operator plays the user, doing in the client what each step's instruction asks and
typing the codes it shows. Runs against a client that skips the handshake, one that
ignores server/unpair, and one that never answers a wrong pairing code show a fault
fails the run. mDNS is not used: multicast is unreliable on CI runners.
"""

import asyncio
import dataclasses
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

from conformance._vendor.aiosendspin.client import ClientListener, PairingSupport, SendspinClient
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
from conformance._vendor.aiosendspin.noise import pairing as noise_pairing
from conformance.validator.report import bundled_aiosendspin

VALIDATOR = Path(sys.executable).with_name("sendspin-validate")
RUN_TIMEOUT_S = 360.0
# The bundled client's attempt timeout, shortened so P4's timeout step runs in seconds.
ATTEMPT_TIMEOUT_S = 6.0
STATIC_CODE = "48151623"
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
# Pairing checks the bundled client must pass, offering the dynamic or the static code.
PAIRING_CHECKS = {
    *(f"PAIR-PSK-00{n}" for n in range(1, 8)),
    *(f"PAIR-DYN-{n:03d}" for n in range(1, 13)),
    *(f"PAIR-DYN-QR-00{n}" for n in range(1, 10)),
    "PAIR-CTL-001",
    "PAIR-CTL-002",
}
STATIC_PAIRING_CHECKS = {
    *(f"PAIR-PSK-00{n}" for n in range(1, 8)),
    *(f"PAIR-STATIC-{n:03d}" for n in range(1, 11)),
    "PAIR-CTL-001",
    "PAIR-CTL-002",
}
# Suite checks the bundled client must pass; U4 is opt-in and runs in its own test.
SUITE_CHECKS = {
    "UNPAIRED-001",
    "UNPAIRED-002",
    "UNPAIRED-003",
    *PAIRING_CHECKS,
    *(f"POSTPAIR-00{n}" for n in range(1, 7)),
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Screen:
    """The bundled client's pairing out-channels: what a person would read or hear."""

    def __init__(self) -> None:
        self.grouped: str | None = None
        self.token: str | None = None
        self.shown: set[str] = set()
        self.languages: tuple[str, ...] = ()

    async def display(self, pairing_code: str | None, *, grouped: str | None) -> None:
        self.grouped = grouped
        if pairing_code is not None and grouped is not None:
            self.shown |= {pairing_code, grouped}

    async def speak(self, pairing_code: str | None, *, languages: tuple[str, ...]) -> None:
        self.languages = languages

    async def show_qr(self, token: str | None) -> None:
        self.token = token
        if token is not None:
            self.shown |= {token, token.removeprefix("SP:")}

    async def read(self, what: str) -> str:
        """Return the code or token on show, once the client shows one."""
        async with asyncio.timeout(10):
            while (value := getattr(self, what)) is None:
                await asyncio.sleep(0.02)
        return value


async def known_good_client(
    identity: Identity, pairing_psk: bytes, screen: Screen, *, static: bool = False
) -> SendspinClient:
    store = InMemoryClientPairingStore()
    await store.set_pairing_psk(PairingPsk(psk_id=psk_id_for(pairing_psk), psk=pairing_psk))
    if static:
        await store.set_static_pairing_code(STATIC_CODE)
        await store.store_pairing_config(
            dataclasses.replace(await store.get_pairing_config(), static_pairing_code_enabled=True)
        )
        support = PairingSupport()
    else:
        support = PairingSupport(
            pairing_code_display=screen.display,
            pairing_code_speaker=screen.speak,
            qr_code_display=screen.show_qr,
        )
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
        pairing_support=support,
    )
    await client.set_unpaired_access(enabled=True)
    return client


class Operator:
    """
    Plays the person running the validator, using the bundled client as their app.

    It reads the validator's output and does in the client what each step asks,
    typing on the validator's input the token and the codes the client shows. In
    client-initiated mode it also keeps the app connected, as a real app reconnects
    on its own. It keeps every long-term PSK the client was given, so a test can
    check none of them leaked.
    """

    def __init__(
        self,
        client: SendspinClient,
        screen: Screen,
        url: str | None,
        token: str,
        keyboard: asyncio.StreamWriter,
    ) -> None:
        self.client = client
        self.screen = screen
        self.long_term_psks: set[str] = set()
        self.transcript: list[str] = []
        self._url = url
        self._token = token
        self._keyboard = keyboard

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
        if "[auto-answered, hidden]" in line and "WRONG" not in line:
            await self._type(await self._secret(line))
        elif line.startswith(("[P3 window]", "[P3 lockout window]", "[P4 timeout window]")):
            self.client.open_pairing_window()
        elif line.startswith(("[U2]", "[U4]")):
            await self.client.set_unpaired_access(enabled=False)
        elif line.startswith("[U3]"):
            await self.client.set_unpaired_access(enabled=True)
        elif line.startswith(("[R1]", "[R4]")):
            for record in await self.client.pairing_store.list_records():
                self.long_term_psks.add(b64url_encode(record.psk))
        elif line.startswith("[R5 restart]"):
            await self.client.disconnect(GoodbyeReason.RESTART)

    async def _secret(self, question: str) -> str:
        if question.startswith("Paste the pairing token"):
            return self._token
        if question.startswith("Scan the QR"):
            return await self.screen.read("token")
        if question.startswith("Type the 8-digit code"):
            return f"{STATIC_CODE[:4]}-{STATIC_CODE[4:]}"
        return await self.screen.read("grouped")

    async def _type(self, answer: str) -> None:
        self._keyboard.write(f"{answer}\n".encode())
        await self._keyboard.drain()

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
        attempt_timeout = mock.patch.object(
            noise_pairing, "_CLIENT_ATTEMPT_TIMEOUT_S", ATTEMPT_TIMEOUT_S
        )
        attempt_timeout.start()
        self.addCleanup(attempt_timeout.stop)
        self.screen = Screen()
        self.client = await known_good_client(self.identity, self.pairing_psk, self.screen)
        self.addAsyncCleanup(self._disconnect)

    async def _disconnect(self) -> None:
        await self.client.disconnect()

    async def _use_static_client(self) -> None:
        await self.client.disconnect()
        self.client = await known_good_client(
            self.identity, self.pairing_psk, self.screen, static=True
        )

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

    async def test_static_code_server_initiated(self) -> None:
        await self._use_static_client()
        url = await self._listen()
        await self._finish(
            await self._run(None, "--connect", url, "--suite", "pairing"),
            expected=STATIC_PAIRING_CHECKS,
        )

    async def test_static_code_client_initiated(self) -> None:
        await self._use_static_client()
        port = free_port()
        await self._finish(
            await self._run(
                f"ws://127.0.0.1:{port}/sendspin", "--port", str(port), "--suite", "pairing"
            ),
            expected=STATIC_PAIRING_CHECKS,
        )

    async def test_client_that_never_answers_a_wrong_code_fails(self) -> None:
        """P2's negative step fails a client that neither retries nor aborts a wrong code."""
        url = await self._listen()
        verify = noise_pairing._run_client_pake  # noqa: SLF001 - the stub wraps the real one

        async def never_answer(*args: object) -> object:
            cpace, verified = await verify(*args)
            if not verified:
                await asyncio.get_running_loop().create_future()
            return cpace, verified

        with mock.patch.object(noise_pairing, "_run_client_pake", never_answer):
            report = await self._report(
                await self._run(None, "--connect", url, "--suite", "pairing")
            )
        self.assertEqual(self.returncode, 1, self.transcript)
        verdicts = {f["id"]: f["verdict"] for f in report["findings"]}
        self.assertEqual(verdicts["PAIR-DYN-004"], "FAIL", self.transcript)
        self.assertEqual(verdicts["PAIR-DYN-QR-004"], "FAIL", self.transcript)
        self.assertEqual(verdicts["PAIR-PSK-007"], "PASS", self.transcript)

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
        validator = await self._start_validator("--verbose", *args)
        assert validator.stdin is not None
        operator = Operator(self.client, self.screen, url, self.token, validator.stdin)
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
            stdin=asyncio.subprocess.PIPE,
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
            *operator.screen.shown,
            STATIC_CODE,
            f"{STATIC_CODE[:4]}-{STATIC_CODE[4:]}",
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
