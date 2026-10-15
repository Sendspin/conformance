"""The suite runner parses `--suite` and keeps a pasted pairing token out of every record."""

import io
import json
import random
import unittest
from collections.abc import Sequence
from typing import Any

from conformance._vendor.aiosendspin.noise import b64url_encode, decode_psk_token
from conformance._vendor.aiosendspin.server import FrameDirection, FramePhase, ObservedFrame
from conformance.validator.code_pairing import CodePairing
from conformance.validator.prompter import Answer, Reply, TerminalPrompter
from conformance.validator.recorder import REDACTED, Recorder
from conformance.validator.suites import Suite, SuiteOptions, SuiteRunner, parse_suites

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


if __name__ == "__main__":
    unittest.main()
