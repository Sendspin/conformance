"""
Pairing with a code: steps P2 (dynamic code), P3 (static code) and P4 (cancel, timeout).

Each step pairs through the validator's server, as P1 does, with a code provider that
asks the user for the code their app shows. Every code the user enters is registered
with the recorder before it is used, so none reaches a record. The steps return their
findings; the suite runner reports them.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from conformance._vendor.aiosendspin.models.types import (
    PairAbortReason,
    PairingCodeFormat,
    PairMethod,
)
from conformance._vendor.aiosendspin.noise import HandshakeAbortedError, PairingError
from conformance._vendor.aiosendspin.noise.pairing import (
    InvalidPairingCodeError,
    PairingAttempt,
    RemotePairingAbortError,
)
from conformance._vendor.aiosendspin.noise.pairing_code import (
    format_pairing_code,
    strip_separators,
)
from conformance._vendor.aiosendspin.noise.pairing_token import (
    decode_pairing_code_token,
    encode_pairing_code_token,
)
from conformance._vendor.aiosendspin.server import SendspinServer
from conformance._vendor.aiosendspin.server.compliance import ClientComplianceError

from . import code_pairing_checks as checks
from .checks import ConnectionRecord
from .code_pairing_checks import CONTROL, DYNAMIC, DYNAMIC_QR, STATIC, Stage
from .findings import Finding, Verdict
from .prompter import Prompter, Reply, human_verdict
from .recorder import EventKind, Recorder

if TYPE_CHECKING:
    from .suites import Link

# The languages the validator may name in server/hello, for a client that speaks the code.
SPOKEN_LANGUAGES = {"de": "German", "es": "Spanish", "fr": "French", "it": "Italian", "nl": "Dutch"}
STEP_CHECKS: dict[str, tuple[str, ...]] = {
    "P2 QR": tuple(f"{DYNAMIC_QR}-00{n}" for n in range(1, 10)),
    "P2": tuple(f"{DYNAMIC}-{n:03d}" for n in range(1, 13)),
    "P3": tuple(f"{STATIC}-00{n}" for n in range(1, 9)),
    "P4 cancel": (f"{CONTROL}-001",),
    "P4 timeout": (f"{CONTROL}-002",),
    "P3 lockout": (f"{STATIC}-009", f"{STATIC}-010"),
}
# How long the client gets to answer something the validator sent.
_RESPONSE_S = 10.0
# How long to watch a connection for a late refusal once a step's exchange is done.
_SETTLE_S = 3.0
_DYNAMIC_DIGITS, _STATIC_DIGITS, _QR_CODE_BYTES = 6, 8, 24
_OPEN_WINDOW = (
    "[P3 window] Open the pairing window on your device (button or gesture), then type the "
    "8-digit code."
)
# What initiate_pairing raises for an attempt that did not pair.
ATTEMPT_ERRORS = (
    PairingError,
    HandshakeAbortedError,
    ClientComplianceError,
    ConnectionError,
    ValueError,
)


def spoken_language(seed: int) -> str:
    """Return the language tag the validator names in server/hello for the run `seed`."""
    return random.Random(seed).choice(sorted(SPOKEN_LANGUAGES))


class Skipped(InvalidPairingCodeError):
    """
    The user skipped entering a code.

    A code provider raises it to end the attempt: aiosendspin leaves pairing cleanly on
    an InvalidPairingCodeError and keeps the connection.
    """


class CodePairing:
    """Runs the pairing-code steps against the client `link` follows."""

    def __init__(
        self,
        server: SendspinServer,
        prompter: Prompter,
        recorder: Recorder,
        link: Link,
        rng: random.Random,
    ) -> None:
        self._server = server
        self._prompter = prompter
        self._recorder = recorder
        self._link = link
        self._rng = rng
        self._static_code: str | None = None

    async def dynamic(
        self, pairing_format: PairingCodeFormat, descriptor: dict[str, Any]
    ) -> list[Finding]:
        """
        P2: pair with a dynamic code in `pairing_format`, entering a wrong code first.

        `descriptor` is the client's `dynamic_pairing_code` entry from its client/hello.
        """
        digits = pairing_format is PairingCodeFormat.DIGITS
        step, prefix = ("P2", DYNAMIC) if digits else ("P2 QR", DYNAMIC_QR)
        number = await self._link.open_connection()
        if number is None:
            return _not_reached(step, "your app did not reconnect in time")
        since = self._link.last_frame(number)
        channels = descriptor.get("out_channels")
        entry = DynamicEntry(
            self._prompter,
            self,
            digits=digits,
            channels=channels if isinstance(channels, list) else [],
            language=(self._server.languages or ("en",))[0],
            mark=lambda: self._link.last_frame(number),
        )
        attempt = PairingAttempt(
            method=PairMethod.DYNAMIC_PAIRING_CODE,
            pairing_code_provider=entry.provide,
            pairing_format=pairing_format,
            on_pair_pending=self._pending(
                "[P2 held back] Your app is holding pairing back until you act on it in the app."
            ),
        )
        task = asyncio.create_task(self._attempt(number, attempt))
        try:
            await _first_of(task, entry.wrong_entered.wait())
            if entry.wrong_entered.is_set() and not await self._link.until(
                number, _answered_after(entry.wrong_mark), _RESPONSE_S
            ):
                await self._end(number)
            error = await task
        finally:
            task.cancel()
        if _mismatched(error) and entry.wrong_entered.is_set():
            self._prompter.instruct(
                "Your app ended that attempt after the wrong code, as it may. The validator "
                "starts a new one, for which your app shows a new code."
            )
            entry.restart()
            error = await self._attempt(number, attempt)
        if isinstance(error, Skipped):
            return checks.not_run(STEP_CHECKS[step], Verdict.SKIPPED, "no code was entered")
        if (
            isinstance(error, RemotePairingAbortError)
            and error.reason is PairAbortReason.ATTEMPT_TIMEOUT
        ):
            self._prompter.instruct(
                "Your app gave up on the attempt before it completed (attempt_timeout). To "
                "run this step again, enter the codes sooner in a new run with --suite pairing."
            )
        await self._link.until(number, lambda r: r.closed, _SETTLE_S)
        await entry.ask_human_checks()
        record = self._link.record(number)
        return _complete(
            step,
            [
                *checks.check_commit(record, since, prefix),
                *checks.check_pairing_index(record, since, f"{prefix}-002"),
                *checks.check_cpace_lengths(record, since, f"{prefix}-003"),
                *(
                    checks.check_wrong_code(record, since, prefix)
                    if entry.wrong_entered.is_set()
                    else checks.not_applicable(
                        [f"{prefix}-004"],
                        "the code entered as wrong was the right one"
                        if entry.right_entered_as_wrong
                        else "not reached: no wrong code was entered",
                    )
                ),
                *checks.check_verification(
                    record, since, {stage: f"{prefix}-00{stage + 5}" for stage in Stage}
                ),
                *entry.findings,
            ],
        )

    async def static(self) -> list[Finding]:
        """P3: pair with the static code, first activating with the pairing window closed."""
        number = await self._link.open_connection()
        if number is None:
            return _not_reached("P3", "your app did not reconnect in time")
        since = self._link.last_frame(number)
        findings: list[Finding] = []

        async def provide() -> str:
            code = await self.ask_code(
                "Type the 8-digit code exactly as your device shows it or as it is printed",
                _STATIC_DIGITS,
            )
            findings.extend(checks.check_presentation(f"{STATIC}-008", code, _STATIC_DIGITS))
            self._static_code = strip_separators(code)
            return code

        attempt = PairingAttempt(
            method=PairMethod.STATIC_PAIRING_CODE,
            pairing_code_provider=provide,
            on_pair_pending=self._pending(_OPEN_WINDOW),
        )
        error = await self._attempt(number, attempt)
        if isinstance(error, Skipped):
            return checks.not_run(STEP_CHECKS["P3"], Verdict.SKIPPED, "no code was entered")
        await self._link.until(number, lambda r: r.closed, _SETTLE_S)
        record = self._link.record(number)
        return _complete(
            "P3",
            [
                *checks.check_static_pending(record, since),
                *checks.check_pairing_index(record, since, f"{STATIC}-002"),
                *checks.check_static_shape(record, since),
                *checks.check_static_order(record, since),
                *checks.check_cpace_lengths(record, since, f"{STATIC}-005"),
                *checks.check_verification(
                    record,
                    since,
                    {Stage.CLIENT_KC: f"{STATIC}-006", Stage.UNWRAP: f"{STATIC}-007"},
                ),
                *findings,
            ],
        )

    async def cancel(self, methods: dict[str, Any]) -> list[Finding]:
        """P4 cancel: start an attempt, then abort it with `user_cancelled` and a new activation."""
        attempt = self._idle_attempt(methods, None)
        number = await self._link.open_connection()
        if attempt is None or number is None:
            why = "the client offers no pairing-code method" if attempt is None else "no connection"
            return _not_reached("P4 cancel", why)
        since = self._link.last_frame(number)
        task = asyncio.create_task(self._attempt(number, attempt))
        try:
            await self._link.until(number, _started_after(since), _RESPONSE_S)
            await self._end(number)
            await task
        finally:
            task.cancel()
        await self._link.until(number, lambda r: r.closed, _SETTLE_S)
        return checks.check_cancel(self._link.record(number), since)

    async def timeout(self, methods: dict[str, Any]) -> list[Finding]:
        """P4 timeout: start an attempt and enter no code, until the client gives up on it."""
        attempt = self._idle_attempt(
            methods,
            "[P4 timeout window] Open the pairing window on your device (button or gesture), "
            "then leave your device alone.",
        )
        number = await self._link.open_connection()
        if attempt is None or number is None:
            why = "the client offers no pairing-code method" if attempt is None else "no connection"
            return _not_reached("P4 timeout", why)
        since = self._link.last_frame(number)
        self._prompter.instruct(
            "[P4 timeout wait] Leave your app alone. The validator enters no code, and waits "
            "up to three minutes for your app to give up on the attempt."
        )
        await self._attempt(number, attempt)
        await self._link.until(number, lambda r: r.closed, _SETTLE_S)
        return checks.check_timeout(self._link.record(number), since)

    async def lockout(self) -> list[Finding]:
        """P3 lockout: five wrong static codes, then a sixth attempt the client must hold back."""
        number = await self._link.open_connection()
        if number is None:
            return _not_reached("P3 lockout", "your app did not reconnect in time")
        since = self._link.last_frame(number)
        for tried in range(checks.LOCKOUT_ATTEMPTS + 1):
            sixth = tried == checks.LOCKOUT_ATTEMPTS
            attempt = PairingAttempt(
                method=PairMethod.STATIC_PAIRING_CODE,
                pairing_code_provider=_never if sixth else self._wrong_static_provider(),
                on_pair_pending=(
                    self._pending(
                        "[P3 lockout window] Open the pairing window on your device again."
                    )
                    if tried == 0
                    else None
                ),
            )
            mark = self._link.last_frame(number)
            task = asyncio.create_task(self._attempt(number, attempt))
            try:
                if tried:
                    # Only the first attempt may wait for a gesture; the sixth enters no code.
                    held = _started_after(mark) if sixth else _held_after(mark)
                    await _first_of(task, self._link.until(number, held, _RESPONSE_S))
                    if not task.done() and (sixth or held(self._link.record(number))):
                        await self._end(number)
                error = await task
            finally:
                task.cancel()
            if sixth or not self._link.is_open(number) or not _mismatched(error):
                break
        await self._link.until(number, lambda r: r.closed, _SETTLE_S)
        return checks.check_lockout(self._link.record(number), since)

    async def ask_code(self, question: str, digits: int, *, expected: str | None = None) -> str:
        """
        Ask until the user enters a code of `digits` digits, and return it as typed.

        The code is registered as a secret, as typed and in each of its forms, before it
        is returned. Raises Skipped if the user skips.
        """
        while True:
            answer = await self._prompter.ask_text(question, expected=expected, secret=True)
            if answer.reply is Reply.RETRY:
                continue
            if answer.reply is not Reply.ANSWERED or answer.value is None:
                raise Skipped("no code was entered")
            typed = answer.value.strip()
            self._register(typed)
            code = strip_separators(typed)
            if len(code) == digits and code.isascii() and code.isdigit():
                self._register(code)
                self._register(format_pairing_code(code))
                return typed
            self._prompter.instruct(f"That is not {digits} digits. Enter it again.")

    async def ask_token(self) -> str:
        """
        Ask for the text of the QR the app shows, and return it as pasted.

        It is registered as a secret, as pasted and in its canonical forms, before it is
        returned. Text that is no pairing token is returned too, for PAIR-DYN-QR-009 to
        fail; entering it ends the attempt. Raises Skipped if the user skips.
        """
        while True:
            answer = await self._prompter.ask_text(
                "Scan the QR your app shows with any QR reader and paste the SP:… text",
                secret=True,
            )
            if answer.reply is not Reply.RETRY:
                break
        if answer.reply is not Reply.ANSWERED or answer.value is None:
            raise Skipped("no code was entered")
        token = answer.value.strip()
        self._register(token)
        try:
            canonical = encode_pairing_code_token(decode_pairing_code_token(token))
        except ValueError:
            return token
        self._register(canonical)
        self._register(canonical.removeprefix("SP:"))
        return token

    def wrong_token(self) -> str:
        """Return a random QR-format code no client shows, registered as a secret."""
        token = encode_pairing_code_token(self._rng.randbytes(_QR_CODE_BYTES))
        self._register(token)
        return token

    def wrong_digits(self) -> str:
        """Return a random 6-digit code, for the auto-answerer to enter as a wrong one."""
        return f"{self._rng.randrange(10**_DYNAMIC_DIGITS):0{_DYNAMIC_DIGITS}d}"

    async def _attempt(self, number: int, attempt: PairingAttempt) -> BaseException | None:
        """Run `attempt` on connection `number`; return why it failed, or None if it paired."""
        try:
            await self._server.initiate_pairing(self._link.client_id, attempt)
        except ATTEMPT_ERRORS as err:
            self._recorder.observe_event(
                number,
                self._link.client_id,
                EventKind.PAIRING_FAILED,
                detail=f"{type(err).__name__}: {err}",
            )
            return err
        self._link.note_keying(number)
        return None

    async def _end(self, number: int) -> None:
        """Cancel the attempt in progress with pair/abort `user_cancelled`, if still connected."""
        if self._link.connection(number) is None:
            return
        try:
            await self._server.end_pairing(self._link.client_id)
        except ATTEMPT_ERRORS as err:
            self._prompter.instruct(f"Cancelling the attempt failed: {err or type(err).__name__}")

    def _register(self, secret: str) -> None:
        self._recorder.add_secret(secret)
        self._recorder.add_secret(secret.upper())

    def _pending(self, instruction: str) -> Callable[[str | None], None]:
        """Return an on_pair_pending callback that tells the user `instruction`."""

        def on_pending(message: str | None) -> None:
            said = f' Your app says: "{message}"' if message else ""
            self._prompter.instruct(f"{instruction}{said}")

        return on_pending

    def _idle_attempt(self, methods: dict[str, Any], gesture: str | None) -> PairingAttempt | None:
        """
        Return an attempt with the client's code method that enters no code, or None.

        A static-code attempt held back asks the user for the window gesture with
        `gesture`, or waits silently when it is None.
        """
        dynamic = methods.get("dynamic_pairing_code")
        formats = dynamic.get("formats") if isinstance(dynamic, dict) else None
        if isinstance(formats, list) and formats:
            return PairingAttempt(
                method=PairMethod.DYNAMIC_PAIRING_CODE,
                pairing_code_provider=_never,
                pairing_format=(
                    PairingCodeFormat.DIGITS if "digits" in formats else PairingCodeFormat.QR_CODE
                ),
            )
        if "static_pairing_code" in methods:
            return PairingAttempt(
                method=PairMethod.STATIC_PAIRING_CODE,
                pairing_code_provider=_never,
                on_pair_pending=self._pending(gesture) if gesture is not None else None,
            )
        return None

    def _wrong_static_provider(self) -> Callable[[], Awaitable[str]]:
        """Return a code provider entering a random static code other than the right one."""
        while True:
            code = f"{self._rng.randrange(10**_STATIC_DIGITS):0{_STATIC_DIGITS}d}"
            if code != self._static_code:
                break
        self._register(code)

        async def provide() -> str:
            return code

        return provide


class DynamicEntry:
    """
    The code provider for a P2 attempt, and the human checks it asks on the way.

    Round 1 asks for the code the app shows, the human checks, then a wrong code, which
    it enters. Round 2 enters the right code: typed again in the digits format, where
    it must match the first, or the pasted QR text again.
    """

    def __init__(
        self,
        prompter: Prompter,
        codes: CodePairing,
        *,
        digits: bool,
        channels: list[Any],
        language: str,
        mark: Callable[[], int],
    ) -> None:
        self.wrong_entered = asyncio.Event()
        """Set once round 1 entered a code other than the one the app showed."""
        self.wrong_mark = 0
        """The last frame recorded before the wrong code was entered."""
        self.right_entered_as_wrong = False
        """Whether the code entered as the wrong one was the code the app showed."""
        self.findings: list[Finding] = []
        """The human checks asked so far, and the QR token check."""
        self._prompter = prompter
        self._codes = codes
        self._digits = digits
        self._channels = channels
        self._language = language
        self._mark = mark
        self._right: str | None = None
        self._first: str | None = None
        self._round = 0

    def restart(self) -> None:
        """Begin a fresh attempt after the client aborted one, with a new code entered as is."""
        self._round = 2
        self._right = None

    async def provide(self) -> str:
        """Return the code to enter for the next round."""
        self._round += 1
        if self._round == 1:
            right = await self._ask_right(
                "Your app should now show (or speak) a 6-digit code. Type it here, exactly as shown"
            )
            self._first = right
            return await self._wrong(right)
        if self._right is None:
            return await self._ask_right("Type the new code your app shows (or speaks)")
        if not self._digits:
            self._prompter.instruct("The validator enters the QR text you pasted again.")
            return self._right
        again = await self._codes.ask_code(
            "Type the code your app shows (or speaks) now", _DYNAMIC_DIGITS
        )
        # Round 2 follows the wrong code, so the app shows the code from round 1 again.
        if self._round == 2:
            same = strip_separators(again) == strip_separators(self._right)
            self.findings.append(
                checks.human(
                    f"{DYNAMIC}-010",
                    Verdict.PASS if same else Verdict.FAIL,
                    "the same code was shown again after the wrong one"
                    if same
                    else "a different code was shown after the wrong one",
                )
            )
        return again

    async def _ask_right(self, question: str) -> str:
        if self._digits:
            self._right = await self._codes.ask_code(question, _DYNAMIC_DIGITS)
        else:
            self._right = await self._codes.ask_token()
            if not self.findings:
                self.findings.extend(checks.check_qr_token(self._right))
        return self._right

    async def ask_human_checks(self) -> None:
        """
        Judge how the first code was shown, and ask how it was spoken.

        Asked once the attempt is over, so the questions do not run down the client's
        attempt timeout. Nothing is asked in the QR format, or when no code was entered.
        """
        typed = self._first
        if not self._digits or typed is None:
            return
        self.findings.extend(
            checks.check_presentation(f"{DYNAMIC}-009", typed, _DYNAMIC_DIGITS)
            if "display" in self._channels
            else checks.not_applicable(
                [f"{DYNAMIC}-009"], "the client does not advertise the display out-channel"
            )
        )
        if "speaker" not in self._channels:
            self.findings.extend(
                checks.not_applicable(
                    [f"{DYNAMIC}-011", f"{DYNAMIC}-012"],
                    "the client does not advertise the speaker out-channel",
                )
            )
            return
        spoke = await self._prompter.confirm("Did your device speak the digits?")
        self.findings.append(
            checks.human(
                f"{DYNAMIC}-011",
                _yes_verdict(spoke.reply, yes=spoke.value is True),
                "the device spoke the code"
                if spoke.value
                else "the device did not speak the code"
                if spoke.reply is Reply.ANSWERED
                else "the user did not say whether the device spoke",
            )
        )
        names = ["English", *SPOKEN_LANGUAGES.values(), "Another language"]
        expected = names.index(SPOKEN_LANGUAGES.get(self._language, "English"))
        heard = await self._prompter.ask_choice(
            "Which language did your device speak the digits in?", names, expected=expected
        )
        verdict = Verdict.SKIPPED if heard.reply is Reply.RETRY else human_verdict(heard, expected)
        # Speaking the server's language is a SHOULD, and only when the device can.
        verdict = Verdict.WARN if verdict is Verdict.FAIL else verdict
        self.findings.append(
            checks.human(
                f"{DYNAMIC}-012",
                verdict,
                {
                    Verdict.PASS: f"the device spoke the server's language, {names[expected]}",
                    Verdict.WARN: f"the device did not speak the server's language, "
                    f"{names[expected]}, which it should when it can",
                }.get(verdict, "the user did not say which language they heard"),
            )
        )

    async def _wrong(self, right: str) -> str:
        """Return a wrong code to enter, marking when it is entered; `right` if none was."""
        if self._digits:
            suggestion = self._codes.wrong_digits()
            wrong = await self._codes.ask_code(
                "Now type a WRONG code: any 6 digits other than the ones shown",
                _DYNAMIC_DIGITS,
                expected=suggestion if suggestion != strip_separators(right) else None,
            )
            if strip_separators(wrong) == strip_separators(right):
                self.right_entered_as_wrong = True
                return right
        else:
            self._prompter.instruct("The validator now enters a wrong code on purpose.")
            wrong = self._codes.wrong_token()
        self.wrong_mark = self._mark()
        self.wrong_entered.set()
        return wrong


def _complete(step: str, findings: list[Finding]) -> list[Finding]:
    """Add an N/A finding for each of `step`'s checks the run never reached."""
    reported = {f.id for f in findings}
    missing = [check_id for check_id in STEP_CHECKS[step] if check_id not in reported]
    return findings + checks.not_applicable(missing, "not reached: the attempt ended first")


def _not_reached(step: str, why: str) -> list[Finding]:
    return checks.not_applicable(STEP_CHECKS[step], f"not reached: {why}")


def _yes_verdict(reply: Reply, *, yes: bool) -> Verdict:
    """PASS for an answered yes, FAIL for an answered no, SKIPPED otherwise."""
    if reply is not Reply.ANSWERED:
        return Verdict.SKIPPED
    return Verdict.PASS if yes else Verdict.FAIL


async def _never() -> str:
    """A code provider for an attempt that must enter no code."""
    await asyncio.get_running_loop().create_future()
    raise AssertionError  # unreachable: the future is never resolved


async def _first_of(task: asyncio.Task[Any], other: Awaitable[Any]) -> None:
    """Return once `task` is done or `other` completes, cancelling `other`."""
    waiter = asyncio.ensure_future(other)
    try:
        await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()


def _mismatched(error: BaseException | None) -> bool:
    return (
        isinstance(error, RemotePairingAbortError)
        and error.reason is PairAbortReason.PAIRING_CODE_MISMATCH
    )


def _answered_after(mark: int) -> Callable[[ConnectionRecord], bool]:
    """Return a condition: the client answered the round run after frame `mark`."""
    answers = {"client/pair-retry", "client/pair-confirm", "pair/abort", "client/goodbye"}
    return lambda record: (
        record.closed or any(f.id > mark and f.inbound and f.type in answers for f in record.frames)
    )


def _started_after(mark: int) -> Callable[[ConnectionRecord], bool]:
    """Return a condition: after frame `mark` an attempt began, was held back, or ended."""
    started = {"client/pair-pending", "client/pair-init", "pair/abort", "client/goodbye"}
    return lambda record: (
        record.closed
        or any(
            f.id > mark
            and (
                (f.inbound and f.type in started)
                or (not f.inbound and f.type == "server/pair-init")
            )
            for f in record.frames
        )
    )


def _held_after(mark: int) -> Callable[[ConnectionRecord], bool]:
    """Return a condition: the client held back an attempt begun after frame `mark`."""
    return lambda record: (
        record.closed
        or any(f.id > mark and f.inbound and f.type == "client/pair-pending" for f in record.frames)
    )
