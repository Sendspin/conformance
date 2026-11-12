"""The suite runner parses `--suite` and keeps a pasted pairing token out of every record."""

import asyncio
import io
import json
import random
import types
import unittest
from collections.abc import Sequence
from typing import Any

from conformance._vendor.aiosendspin.noise import b64url_encode, decode_psk_token
from conformance._vendor.aiosendspin.server import FrameDirection, FramePhase, ObservedFrame
from conformance.validator.code_pairing import CodePairing
from conformance.validator.findings import Finding, Verdict
from conformance.validator.prompter import Answer, AutoAnswerPrompter, Reply, TerminalPrompter
from conformance.validator.recorder import REDACTED, Recorder
from conformance.validator.suites import (
    STEP_CHECKS,
    Suite,
    SuiteOptions,
    SuiteRunner,
    parse_suites,
)

VECTOR_QR_TOKEN = "SP:14DQ6FY7E4XTOP9HJ5LV6Z3PO57YPD4XT6T97N5Y"
VECTOR_TOKEN = (
    "SP:0AAAQEAYEAUDAOCAJBIFQYDIOB4IBCEQTCQKRMFYYDENBWHA5DYP6BYPC4PSOLZXH5DU6V97M5XXO74HR6LZ"
    "7J5PW674PT6X37T6757Y"
)


class ParseSuitesTests(unittest.TestCase):
    def test_names_in_any_order(self) -> None:
        self.assertEqual(
            parse_suites("post-pairing, unpaired"), {Suite.POST_PAIRING, Suite.UNPAIRED}
        )

    def test_unknown_or_empty_raises(self) -> None:
        for text in ("pairing,player", "", " , "):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_suites(text)


class PastingPrompter(TerminalPrompter):
    """Answers each text question with the next of `pastes`."""

    def __init__(self, pastes: Sequence[str]) -> None:
        super().__init__(output=io.StringIO())
        self._pastes = iter(pastes)

    async def ask_text(
        self, question: str, *, expected: str | None = None, secret: bool = False
    ) -> Answer[str]:
        return Answer(Reply.ANSWERED, next(self._pastes))


class TokenEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pasted_token_and_its_psk_are_redacted_before_pairing(self) -> None:
        verbose = io.StringIO()
        recorder = Recorder(verbose=verbose)
        typo = "SP:0AAAQ"
        pasted = f"  {VECTOR_TOKEN.lower()}\n"
        runner = SuiteRunner(
            server=None,  # type: ignore[arg-type]
            prompter=PastingPrompter([typo, pasted]),
            recorder=recorder,
            store=None,  # type: ignore[arg-type]
            link=None,  # type: ignore[arg-type]
            options=SuiteOptions(suites=frozenset(Suite), fault_injection=False),
        )
        token = await runner._ask_token()  # noqa: SLF001 - the registration is the behaviour

        self.assertEqual(token, pasted.strip())
        psk = b64url_encode(decode_psk_token(VECTOR_TOKEN).pairing_psk)
        message: dict[str, Any] = {
            "type": "client/goodbye",
            "payload": {"reason": f"{typo} {pasted.strip()} {psk}"},
        }
        recorder.observe_frame(
            ObservedFrame(
                connection=1,
                client_id="client",
                direction=FrameDirection.IN,
                phase=FramePhase.TRANSPORT,
                data=b"\x00" + json.dumps(message).encode(),
                monotonic_us=1,
            )
        )
        self.assertEqual(
            recorder.frames[0].payload, {"reason": f"{REDACTED} {REDACTED} {REDACTED}"}
        )
        for secret in (typo, pasted.strip(), psk):
            self.assertNotIn(secret, verbose.getvalue())


class CodeEntryTests(unittest.IsolatedAsyncioTestCase):
    """Every form of an entered code is registered before the code is used."""

    def entry(self, pastes: Sequence[str]) -> tuple[CodePairing, Recorder, io.StringIO]:
        verbose = io.StringIO()
        recorder = Recorder(verbose=verbose)
        codes = CodePairing(
            server=None,  # type: ignore[arg-type]
            prompter=PastingPrompter(pastes),
            recorder=recorder,
            link=None,  # type: ignore[arg-type]
            rng=random.Random(1),
        )
        return codes, recorder, verbose

    def record(self, recorder: Recorder, text: str) -> dict[str, Any] | None:
        message = {"type": "client/pair-pending", "payload": {"message": text}}
        recorder.observe_frame(
            ObservedFrame(
                connection=1,
                client_id="client",
                direction=FrameDirection.IN,
                phase=FramePhase.TRANSPORT,
                data=b"\x00" + json.dumps(message).encode(),
                monotonic_us=1,
            )
        )
        return recorder.frames[-1].payload

    async def test_a_typed_code_is_redacted_in_every_form(self) -> None:
        codes, recorder, verbose = self.entry(["12-34", " 123 456 "])
        typed = await codes.ask_code("Type the code", 6)

        self.assertEqual(typed, "123 456")
        payload = self.record(recorder, "12-34 123456 123-456 123 456")
        self.assertEqual(payload, {"message": " ".join([REDACTED] * 4)})
        self.assertNotIn("456", verbose.getvalue())

    async def test_a_pasted_qr_token_is_redacted_in_every_form(self) -> None:
        pasted = VECTOR_QR_TOKEN.lower()
        codes, recorder, verbose = self.entry([pasted])
        self.assertEqual(await codes.ask_token(), pasted)

        body = VECTOR_QR_TOKEN.removeprefix("SP:")
        payload = self.record(recorder, f"{pasted} {VECTOR_QR_TOKEN} {body}")
        self.assertEqual(payload, {"message": " ".join([REDACTED] * 3)})
        self.assertNotIn(body, verbose.getvalue())


    async def test_text_that_is_no_token_is_returned_for_judging(self) -> None:
        codes, recorder, _ = self.entry(["SP:0NOTAQRCODE"])
        self.assertEqual(await codes.ask_token(), "SP:0NOTAQRCODE")
        payload = self.record(recorder, "SP:0NOTAQRCODE")
        self.assertEqual(payload, {"message": REDACTED})



def goodbye(recorder: Recorder, reason: str, *, client_id: str = "client") -> None:
    """Record a client/goodbye with `reason`, as the server's frame observer would."""
    message = {"type": "client/goodbye", "payload": {"reason": reason}}
    recorder.observe_frame(
        ObservedFrame(
            connection=2,
            client_id=client_id,
            direction=FrameDirection.IN,
            phase=FramePhase.TRANSPORT,
            data=b"\x00" + json.dumps(message).encode(),
            monotonic_us=1,
        )
    )

class StepErrorTests(unittest.IsolatedAsyncioTestCase):
    """A step that raises is reported as a tool error, and the session goes on."""

    def runner(self) -> SuiteRunner:
        return SuiteRunner(
            server=None,  # type: ignore[arg-type]
            prompter=AutoAnswerPrompter(output=io.StringIO()),
            recorder=Recorder(verbose=io.StringIO()),
            store=None,  # type: ignore[arg-type]
            link=types.SimpleNamespace(client_id="client"),  # type: ignore[arg-type]
            options=SuiteOptions(suites=frozenset(Suite), fault_injection=False),
        )

    async def test_a_raising_step_reports_tool_errors_and_the_next_step_runs(self) -> None:
        runner = self.runner()
        ran: list[str] = []

        async def broken() -> list[Finding]:
            raise ValueError("zip() argument 2 is longer than argument 1")

        async def fine() -> list[Finding]:
            ran.append("R1")
            return []

        with self.assertLogs("conformance.validator.suites", "ERROR") as logs:
            await runner._step("P2", "x", broken)  # noqa: SLF001 - the step loop is the behaviour
        await runner._step("R1", "y", fine)  # noqa: SLF001

        findings = runner._findings  # noqa: SLF001
        self.assertEqual([f.id for f in findings], list(STEP_CHECKS["P2"]))
        self.assertEqual({f.verdict for f in findings}, {Verdict.TOOL_ERROR})
        self.assertIn("ValueError: zip() argument 2", findings[0].message)
        self.assertIn("Traceback", logs.output[0])
        self.assertEqual(ran, ["R1"])

    async def test_a_client_busy_with_another_server_is_a_tool_error_not_a_fail(self) -> None:
        runner = self.runner()
        recorder = runner._recorder  # noqa: SLF001
        output = runner._prompter._output  # type: ignore[attr-defined] # noqa: SLF001

        async def turned_away() -> list[Finding]:
            goodbye(recorder, "concurrent_attempt")
            return [
                Finding("UNPAIRED-002", Verdict.FAIL, "ref", "wrong goodbye"),
                Finding("UNPAIRED-001", Verdict.PASS, "ref", "fine"),
            ]

        await runner._step("U2", "x", turned_away)  # noqa: SLF001
        await runner._step("U3", "y", turned_away)  # noqa: SLF001

        busy, kept, *_ = runner._findings  # noqa: SLF001
        self.assertIs(busy.verdict, Verdict.TOOL_ERROR)
        self.assertIn("concurrent_attempt", busy.message)
        self.assertIn("was FAIL: wrong goodbye", busy.message)
        self.assertEqual(busy.frames, (recorder.frames[0].id,))
        self.assertIs(kept.verdict, Verdict.PASS)
        self.assertEqual(output.getvalue().count("another server is connected"), 1)

    async def test_another_clients_goodbye_leaves_the_findings_alone(self) -> None:
        runner = self.runner()
        failed = Finding("UNPAIRED-001", Verdict.FAIL, "ref", "real failure")

        async def fails() -> list[Finding]:
            goodbye(runner._recorder, "concurrent_attempt", client_id="other")  # noqa: SLF001
            return [failed]

        await runner._step("U1", "x", fails)  # noqa: SLF001
        self.assertEqual(runner._findings, [failed])  # noqa: SLF001

    async def test_a_goodbye_from_an_earlier_step_does_not_taint_the_next(self) -> None:
        runner = self.runner()
        failed = Finding("UNPAIRED-001", Verdict.FAIL, "ref", "real failure")

        async def fails() -> list[Finding]:
            return [failed]

        goodbye(runner._recorder, "concurrent_attempt")  # noqa: SLF001
        await runner._step("U1", "x", fails)  # noqa: SLF001
        self.assertEqual(runner._findings, [failed])  # noqa: SLF001

    async def test_cancellation_still_propagates(self) -> None:
        async def cancelled() -> list[Finding]:
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await self.runner()._step("R1", "y", cancelled)  # noqa: SLF001



class Terminal(io.StringIO):
    """Output that reports itself as a terminal."""

    def isatty(self) -> bool:
        return True


class ScreenTests(unittest.IsolatedAsyncioTestCase):
    """Each step starts on a cleared terminal that still shows the last step's results."""

    def runner(self, output: io.StringIO) -> SuiteRunner:
        return SuiteRunner(
            server=None,  # type: ignore[arg-type]
            prompter=AutoAnswerPrompter(output=output),
            recorder=Recorder(verbose=io.StringIO()),
            store=None,  # type: ignore[arg-type]
            link=None,  # type: ignore[arg-type]
            options=SuiteOptions(suites=frozenset(Suite), fault_injection=False),
        )

    async def test_a_step_clears_the_terminal_and_recaps_the_last_step(self) -> None:
        output = Terminal()
        runner = self.runner(output)

        async def passes() -> list[Finding]:
            return [Finding("UNPAIRED-001", Verdict.PASS, "ref", "sentinel handshake")]

        await runner._step("U1", "first", passes)  # noqa: SLF001
        await runner._step("U2", "second", passes)  # noqa: SLF001

        screens = output.getvalue().split("\x1b[H\x1b[2J")
        self.assertEqual(len(screens), 3)
        self.assertNotIn("sentinel handshake", screens[1].split("[U1]")[0])
        self.assertIn("Since the last step:", screens[2])
        self.assertLess(
            screens[2].index("sentinel handshake"), screens[2].index("[U2] second")
        )

    async def test_piped_output_is_never_cleared_or_repeated(self) -> None:
        output = io.StringIO()
        runner = self.runner(output)

        async def passes() -> list[Finding]:
            return [Finding("UNPAIRED-001", Verdict.PASS, "ref", "sentinel handshake")]

        await runner._step("U1", "first", passes)  # noqa: SLF001
        await runner._step("U2", "second", passes)  # noqa: SLF001

        self.assertNotIn("\x1b", output.getvalue())
        self.assertEqual(output.getvalue().count("sentinel handshake"), 2)


if __name__ == "__main__":
    unittest.main()
