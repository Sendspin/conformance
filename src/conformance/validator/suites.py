"""
The unpaired, pairing and post-pairing suites: steps U1–U4, P1–P4 and R1–R5.

Each step tells the user what happens next and waits until they are ready, drives
the client through the validator's server, then judges the connection it ran on
with the checks in `suite_checks`, printing each verdict as it resolves.
"""

from __future__ import annotations

import asyncio
import random
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from conformance._vendor.aiosendspin.models.core import (
    ActivatePairing,
    ServerActivateMessage,
    ServerActivatePayload,
)
from conformance._vendor.aiosendspin.models.types import (
    Activity,
    ConnectionReason,
    PairingCodeFormat,
    PairMethod,
)
from conformance._vendor.aiosendspin.noise import (
    b64url_encode,
    decode_psk_token,
)
from conformance._vendor.aiosendspin.noise.pairing import PairingAttempt
from conformance._vendor.aiosendspin.server import SendspinServer
from conformance._vendor.aiosendspin.server.connection import SendspinConnection

from . import code_pairing, code_pairing_checks, suite_checks
from .checks import ConnectionRecord
from .code_pairing import CodePairing
from .findings import Evidence, Finding, Verdict
from .prompter import Prompter, Reply
from .recorder import EventKind, Recorder
from .storage import AdmitUnpairedStore

# How long the client gets to answer something the validator sent.
_RESPONSE_S = 10.0
# How long to watch a connection for a late refusal once a step's exchange is done.
_SETTLE_S = 3.0
# How many connections a step tries before giving up on one that suits it.
_MATCH_ATTEMPTS = 5
# The range of the random output delay R5 sets before the restart, in milliseconds.
_DELAY_MIN_MS, _DELAY_SPAN_MS = 100, 400


class Suite(StrEnum):
    """A group of steps `--suite` selects; they run in declaration order."""

    UNPAIRED = "unpaired"
    PAIRING = "pairing"
    POST_PAIRING = "post-pairing"


STEP_CHECKS: dict[str, tuple[str, ...]] = {
    "U1": ("UNPAIRED-001",),
    "U2": ("UNPAIRED-002",),
    "U3": ("UNPAIRED-003",),
    "U4": ("UNPAIRED-004",),
    "P1": tuple(f"PAIR-PSK-00{n}" for n in range(1, 8)),
    **code_pairing.STEP_CHECKS,
    "R1": ("POSTPAIR-001",),
    "R3": ("POSTPAIR-002",),
    "R4": ("POSTPAIR-003", "POSTPAIR-004"),
    "R5": ("POSTPAIR-005", "POSTPAIR-006"),
}


def parse_suites(text: str) -> frozenset[Suite]:
    """
    Parse a comma-separated `--suite` value.

    Raises ValueError for an empty value or one naming an unknown suite.
    """
    names = [name.strip() for name in text.split(",") if name.strip()]
    known = ", ".join(suite.value for suite in Suite)
    if not names:
        msg = f"name at least one suite: {known}"
        raise ValueError(msg)
    try:
        return frozenset(Suite(name) for name in names)
    except ValueError:
        msg = f"unknown suite in {text!r}; choose from {known}"
        raise ValueError(msg) from None


@dataclass(frozen=True, slots=True)
class SuiteOptions:
    """
    Which suites to run, and how.

    `fault_injection` allows steps that send what no client may accept. `seed` drives
    every randomized challenge, so a run can be replayed.
    """

    suites: frozenset[Suite]
    fault_injection: bool
    seed: int = 0


class Link:
    """
    The client under test's connections: which one is current, and how to get another.

    In server-initiated mode (`url` set) the validator dials the client again; in
    client-initiated mode it waits for the app to reconnect. Every wait is bounded by
    `timeout_s`, and a connection that never came is reported as None.
    """

    def __init__(
        self,
        server: SendspinServer,
        prompter: Prompter,
        recorder: Recorder,
        *,
        client_id: str,
        url: str | None,
        timeout_s: float,
    ) -> None:
        self.client_id = client_id
        self._server = server
        self._prompter = prompter
        self._recorder = recorder
        self._url = url
        self._timeout_s = timeout_s

    @property
    def current(self) -> int | None:
        """The client's most recently keyed connection, open or not."""
        keyed = [
            e.connection
            for e in self._recorder.events
            if e.client_id == self.client_id and e.kind is EventKind.KEYED
        ]
        return keyed[-1] if keyed else None

    def record(self, number: int) -> ConnectionRecord:
        """Return everything recorded so far on connection `number`."""
        return ConnectionRecord.of(
            number, self._recorder.frames, self._recorder.noncompliance, self._recorder.events
        )

    def is_open(self, number: int | None) -> bool:
        """Whether connection `number` completed its handshake and is neither ended nor ending."""
        if number is None:
            return False
        record = self.record(number)
        return (
            any(e.kind is EventKind.KEYED for e in record.events)
            and not record.closed
            and not record.inbound("client/goodbye")
        )

    def connection(self, number: int) -> SendspinConnection | None:
        """Return aiosendspin's connection `number`, if it is the client's attached one."""
        client = self._server.get_client(self.client_id)
        connection = client.connection if client is not None else None
        if connection is None or connection.connection_number != number:
            return None
        return connection

    def connections_since(self, watermark: int) -> list[int]:
        """Return the client's connections numbered above `watermark`, by their client/init."""
        return sorted(
            {
                f.connection
                for f in self._recorder.frames
                if f.connection > watermark
                and f.type == "client/init"
                and (f.payload or {}).get("client_id") == self.client_id
            }
        )

    def last_frame(self, number: int) -> int:
        """Return the id of the last frame recorded on connection `number`, or 0."""
        frames = self.record(number).frames
        return frames[-1].id if frames else 0

    def note_keying(self, number: int) -> None:
        """Record which PSK keys connection `number` now, after a re-handshake."""
        connection = self.connection(number)
        if connection is not None and connection.psk_category is not None:
            self._recorder.observe_event(
                number,
                self.client_id,
                EventKind.KEYED,
                psk_category=connection.psk_category.value,
            )

    async def until(
        self, number: int, condition: Callable[[ConnectionRecord], bool], timeout_s: float
    ) -> bool:
        """Wait until `condition` holds for connection `number`; False if time ran out."""
        try:
            async with asyncio.timeout(timeout_s):
                await self._recorder.wait_until(lambda: condition(self.record(number)))
        except TimeoutError:
            return False
        return True

    async def open_connection(self) -> int | None:
        """Return the current connection if it is open, or else a new one."""
        if self.is_open(self.current):
            return self.current
        return await self.reconnect()

    async def reconnect(self) -> int | None:
        """Close the current connection, then return the client's next one, or None."""
        await self._close()
        return await self._next(self._watermark(), self._timeout_s)

    async def await_reconnect(self, previous: int) -> int | None:
        """
        Wait for connection `previous` to end, then return the client's next one.

        For a restart the user performs: nothing is closed by the validator. None when
        `previous` stays open or no new connection comes in time.
        """
        if not await self.until(previous, lambda r: r.closed, self._timeout_s):
            return None
        return await self._next(previous, self._timeout_s)

    async def connect_matching(
        self, holds: Callable[[ConnectionRecord], bool], mismatch: str
    ) -> int | None:
        """
        Return an open connection whose record satisfies `holds`, or None.

        When the current connection does not, the validator reconnects. Each new
        connection that still does not is reported to the user as `mismatch`, and they
        are asked whether to try again, up to a few times. None when they stop, the
        attempts run out, or the app does not reconnect in time.
        """
        number = self.current
        for attempt in range(_MATCH_ATTEMPTS):
            if self.is_open(number) and holds(self.record(number)):
                return number
            if attempt:
                self._prompter.instruct(mismatch)
                answer = await self._prompter.confirm("Try again?")
                if answer.reply is not Reply.RETRY and not (
                    answer.reply is Reply.ANSWERED and answer.value
                ):
                    return None
            number = await self.reconnect()
            if number is None:
                return None
        return number if self.is_open(number) and holds(self.record(number)) else None

    async def _close(self) -> None:
        """Close the current connection, if open, and stop any dialing toward the client."""
        number = self.current
        if self._url is not None:
            self._server.disconnect_from_client(self._url)
        elif number is not None and (connection := self.connection(number)) is not None:
            await connection.disconnect(retry_connection=False)
        if self.is_open(number):
            assert number is not None
            await self.until(number, lambda r: r.closed, _RESPONSE_S)

    async def _next(self, watermark: int, timeout_s: float) -> int | None:
        """Return the client's first connection numbered above `watermark`, dialing if we dial."""
        if self._url is None:
            self._prompter.instruct(
                "Waiting for your app to reconnect to the validator; most apps do so on their own."
            )
        try:
            async with asyncio.timeout(timeout_s):
                if self._url is not None and not self.connections_since(watermark):
                    await self._server.connect_to_client_and_wait(
                        self._url,
                        connection_reason=ConnectionReason.PLAYBACK,
                        retry_initial_connection=True,
                    )
                await self._recorder.wait_until(lambda: self._settled(watermark))
        except TimeoutError:
            pass
        new = self.connections_since(watermark)
        keyed = [n for n in new if self._keyed(n)]
        return (keyed or new or [None])[0]

    def _settled(self, watermark: int) -> bool:
        """Whether a new connection was keyed, or the first new one is known to have failed."""
        new = self.connections_since(watermark)
        if any(self._keyed(n) for n in new):
            return True
        # A second new connection means the app gave up on the first and tried again.
        return bool(new) and (self.record(new[0]).rejected or len(new) > 1)

    def _keyed(self, number: int) -> bool:
        return any(
            e.connection == number and e.kind is EventKind.KEYED for e in self._recorder.events
        )

    def _watermark(self) -> int:
        return max((f.connection for f in self._recorder.frames), default=0)


class SuiteRunner:
    """Runs the selected suites against the client `link` follows and collects their findings."""

    def __init__(
        self,
        server: SendspinServer,
        prompter: Prompter,
        recorder: Recorder,
        store: AdmitUnpairedStore,
        link: Link,
        options: SuiteOptions,
    ) -> None:
        self._server = server
        self._prompter = prompter
        self._recorder = recorder
        self._store = store
        self._link = link
        self._options = options
        self._findings: list[Finding] = []
        self._codes = CodePairing(server, prompter, recorder, link, random.Random(options.seed))

    async def run(self) -> list[Finding]:
        """Run every selected suite in order and return the findings, as reported."""
        if Suite.UNPAIRED in self._options.suites:
            await self._unpaired()
        if Suite.PAIRING in self._options.suites:
            await self._pairing()
        if Suite.POST_PAIRING in self._options.suites:
            await self._post_pairing()
        return self._findings

    async def _step(
        self,
        step: str,
        instruction: str,
        run: Callable[[], Awaitable[list[Finding]]],
        *,
        evidence: Evidence | None = None,
    ) -> None:
        """
        Show `step`, wait until the user is ready, run it and report its findings.

        `evidence` replaces what each finding rests on, when the whole step rests on it.
        """
        while True:
            self._prompter.instruct(f"\n[{step}] {instruction}")
            answer = await self._prompter.confirm("Ready?")
            if answer.reply is not Reply.RETRY:
                break
        if answer.reply is Reply.ANSWERED and answer.value:
            findings = await run()
        else:
            findings = _not_run(step, Verdict.SKIPPED, f"the user skipped {step}")
        self._report([replace(f, evidence=evidence) if evidence else f for f in findings])

    def _report(self, findings: Sequence[Finding]) -> None:
        for finding in findings:
            self._prompter.report_check(finding)
            self._findings.append(finding)

    async def _unpaired(self) -> None:
        """Run U1–U4, restoring afterwards a pairing an earlier run left, which U1 forgets."""
        earlier = await self._store.record_by_client_id(self._link.client_id)
        try:
            await self._step("U1", _U1, self._u1)
            await self._step("U2", _U2, self._u2, evidence=Evidence.HUMAN)
            if self._options.fault_injection:
                await self._step("U4", _U4, self._u4)
            else:
                self._report(
                    suite_checks.not_run(
                        STEP_CHECKS["U4"],
                        Verdict.NOT_APPLICABLE,
                        "fault injection is opt-in: run with --fault-injection",
                    )
                )
            await self._step("U3", _U3, self._u3, evidence=Evidence.HUMAN)
        finally:
            if earlier is not None:
                await self._store.store_record(earlier)

    async def _u1(self) -> list[Finding]:
        number = self._link.current
        if await self._store.record_by_client_id(self._link.client_id) is not None:
            self._prompter.instruct(
                "The validator paired with your app in an earlier run. It forgets that pairing "
                "until the unpaired steps are done, and reconnects unpaired."
            )
            await self._store.remove_record(self._link.client_id)
            number = await self._link.reconnect()
        if number is None:
            return _not_reached("U1", "your app did not reconnect in time")
        return suite_checks.check_sentinel_handshake(self._link.record(number))

    async def _u2(self) -> list[Finding]:
        number = await self._link.connect_matching(
            lambda record: suite_checks.unpaired_access(record) is False,
            "Your app still reports unpaired access on. Turn it off, then try again.",
        )
        if number is None:
            return _not_reached("U2", "no connection reported unpaired access off in time")
        roles = _preferred_roles(self._link.record(number))
        payload = ServerActivatePayload(activities=[Activity.PLAYBACK], active_roles=roles)
        if not await self._declare(number, payload):
            return _not_reached("U2", "the connection ended before the validator could activate")
        await self._link.until(number, _ended_or_said({"client/goodbye"}), _RESPONSE_S)
        return suite_checks.check_pairing_required(self._link.record(number))

    async def _u4(self) -> list[Finding]:
        number = await self._link.connect_matching(
            lambda record: suite_checks.unpaired_access(record) is False,
            "Your app reports unpaired access on. Turn it off for this step, then try again.",
        )
        if number is None:
            return _not_reached("U4", "no connection reported unpaired access off in time")
        payload = ServerActivatePayload(
            activities=[Activity.PLAYBACK, Activity.PAIRING],
            active_roles=[],
            pairing=ActivatePairing(method=PairMethod.PAIRING_PSK),
        )
        if not await self._declare(number, payload):
            return _not_reached("U4", "the connection ended before the validator could activate")
        await self._link.until(
            number, _ended_or_said({"client/goodbye", "pair/abort"}), _RESPONSE_S
        )
        return suite_checks.check_inadmissible_activation(self._link.record(number))

    async def _u3(self) -> list[Finding]:
        number = await self._link.connect_matching(
            lambda record: suite_checks.unpaired_access(record) is True,
            "Your app still reports unpaired access off. Turn it on, then try again.",
        )
        if number is None:
            return _not_reached("U3", "no connection reported unpaired access on in time")
        await self._link.until(
            number, _ended_or_said({"client/state", "client/goodbye"}), _RESPONSE_S
        )
        return suite_checks.check_unpaired_playback(self._link.record(number))

    async def _pairing(self) -> None:
        """Run each pairing method the client offers: QR code, pairing_psk, then its code."""
        number = self._link.current
        if number is None:
            self._report(
                [
                    finding
                    for step in ("P2 QR", "P1", "P2", "P3", "P4 cancel", "P4 timeout", "P3 lockout")
                    for finding in _not_reached(
                        step, "no connection of the client completed its handshake"
                    )
                ]
            )
            return
        methods = _pair_methods(self._link.record(number))
        dynamic = methods.get("dynamic_pairing_code")
        dynamic = dynamic if isinstance(dynamic, dict) else {}
        formats = dynamic.get("formats") if isinstance(dynamic.get("formats"), list) else []
        codes = self._codes
        await self._offered(
            "P2 QR",
            "qr_code" in formats,
            "dynamic_pairing_code in the qr_code format",
            lambda: codes.dynamic(PairingCodeFormat.QR_CODE, dynamic),
        )
        await self._offered("P1", "pairing_psk" in methods, "pairing_psk", self._p1)
        await self._offered(
            "P2",
            "digits" in formats,
            "dynamic_pairing_code in the digits format",
            lambda: codes.dynamic(PairingCodeFormat.DIGITS, dynamic),
        )
        static = "static_pairing_code" in methods
        await self._offered("P3", static, "static_pairing_code", codes.static)
        coded = bool(formats) or static
        await self._offered(
            "P4 cancel", coded, "a pairing-code method", lambda: codes.cancel(methods)
        )
        await self._offered(
            "P4 timeout", coded, "a pairing-code method", lambda: codes.timeout(methods)
        )
        await self._offered("P3 lockout", static, "static_pairing_code", codes.lockout)

    async def _offered(
        self, step: str, offered: bool, method: str, run: Callable[[], Awaitable[list[Finding]]]
    ) -> None:
        """Run `step` if the client's hello offers `method`, or report its checks N/A."""
        if offered:
            await self._step(step, _INSTRUCTIONS[step], run)
        else:
            self._report(_not_reached(step, f"the client's hello does not offer {method}"))

    async def _p1(self) -> list[Finding]:
        token = await self._ask_token()
        if token is None:
            return suite_checks.not_run(
                STEP_CHECKS["P1"], Verdict.SKIPPED, "no pairing token was entered"
            )
        findings = suite_checks.check_pairing_token(token, self._link.client_id)
        if any(f.verdict is not Verdict.PASS for f in findings):
            return findings + _not_reached("P1", "no usable pairing token", skip=2)
        pairing_psk = decode_psk_token(token).pairing_psk
        number = await self._link.open_connection()
        if number is None:
            return findings + _not_reached("P1", "your app did not reconnect in time", skip=2)
        since = self._link.last_frame(number)
        attempt = PairingAttempt(
            method=PairMethod.PAIRING_PSK, pairing_psk=pairing_psk, client_id=self._link.client_id
        )
        try:
            await self._server.initiate_pairing(self._link.client_id, attempt)
        except code_pairing.ATTEMPT_ERRORS as err:
            self._prompter.instruct(f"Pairing did not complete: {err or type(err).__name__}")
        else:
            self._link.note_keying(number)
        await self._link.until(number, lambda r: r.closed, _SETTLE_S)
        record = self._link.record(number)
        return [
            *findings,
            *suite_checks.check_pairing_rehandshake(record, since),
            *suite_checks.check_pair_init(record, since),
            *suite_checks.check_pair_finalize(record, since),
            *suite_checks.check_promotion(record, since),
            *suite_checks.check_paired_activation(record, since),
        ]

    async def _ask_token(self) -> str | None:
        """Ask until the user pastes a decodable pairing token; None if they skip."""
        while True:
            answer = await self._prompter.ask_text(
                "Paste the pairing token (SP:…) from your app or leaflet. If you only have a "
                "QR code, scan it with any QR reader and paste the text",
                secret=True,
            )
            if answer.reply is Reply.RETRY:
                continue
            if answer.reply is not Reply.ANSWERED or answer.value is None:
                return None
            token = answer.value.strip()
            # Before anything else is recorded, so the token and its PSK are never logged.
            self._recorder.add_secret(token)
            try:
                decoded = decode_psk_token(token)
            except ValueError as err:
                self._prompter.instruct(f"That is not a pairing token ({err}). Paste it again.")
                continue
            self._recorder.add_secret(b64url_encode(decoded.pairing_psk))
            return token

    async def _post_pairing(self) -> None:
        paired = await self._store.record_by_client_id(self._link.client_id)
        if paired is None:
            self._report(
                [
                    finding
                    for step in ("R1", "R3", "R5", "R4")
                    for finding in _not_reached(step, "the validator is not paired with the client")
                ]
            )
            return
        await self._step("R1", _R1, self._r1)
        await self._step("R3", _R3, self._r3)
        await self._step("R5", _R5, self._r5, evidence=Evidence.HUMAN)
        await self._step("R4", _R4, self._r4)

    async def _r1(self) -> list[Finding]:
        number = await self._link.reconnect()
        if number is None:
            return _not_reached("R1", "your app did not reconnect in time")
        await self._link.until(number, _ended_or_activated, _RESPONSE_S)
        return suite_checks.check_long_term_reconnect(self._link.record(number))

    async def _r3(self) -> list[Finding]:
        record = await self._store.record_by_client_id(self._link.client_id)
        if record is None:
            return _not_reached("R3", "the validator is not paired with the client")
        await self._store.remove_record(self._link.client_id)
        self._store.admit_unpaired = False
        number = None
        try:
            number = await self._link.reconnect()
            if number is not None:
                await self._link.until(number, _ended_or_activated, _RESPONSE_S)
        finally:
            await self._store.store_record(record)
            self._store.admit_unpaired = True
        if number is None:
            return _not_reached("R3", "your app did not reconnect in time")
        return suite_checks.check_forgotten_reconnect(self._link.record(number))

    async def _r5(self) -> list[Finding]:
        number = await self._link.reconnect()
        if number is None:
            return _not_reached("R5", "your app did not reconnect in time")
        await self._link.until(number, _ended_or_said({"client/state"}), _RESPONSE_S)
        delay = await self._set_output_delay(number)
        self._prompter.instruct(
            "[R5 restart] Now fully quit your app and start it again. Closing a window is not "
            "enough."
        )
        answer = await self._prompter.confirm("Has your app restarted?")
        if answer.reply is not Reply.ANSWERED or not answer.value:
            return suite_checks.not_run(
                STEP_CHECKS["R5"], Verdict.SKIPPED, "the user did not restart the app"
            )
        restarted = await self._link.await_reconnect(number)
        if restarted is None:
            return _not_reached("R5", "your app did not disconnect and reconnect in time")
        await self._link.until(restarted, _ended_or_said({"client/state"}), _RESPONSE_S)
        after = self._link.record(restarted)
        return [
            *suite_checks.check_restart_keeps_pairing(after),
            *suite_checks.check_restart_keeps_output_delay(self._link.record(number), after, delay),
        ]

    async def _r4(self) -> list[Finding]:
        record = await self._store.record_by_client_id(self._link.client_id)
        number = await self._link.open_connection()
        if record is None or number is None:
            return _not_reached("R4", "no paired connection to unpair")
        try:
            await self._server.unpair(self._link.client_id)
        except ValueError:
            return _not_reached("R4", "the client disconnected before server/unpair")
        try:
            await self._link.until(number, lambda r: r.closed, _RESPONSE_S)
            findings = suite_checks.check_unpair_goodbye(self._link.record(number))
            # Referencing the unpaired record shows whether the client really dropped it.
            await self._store.store_record(record)
            again = await self._link.reconnect()
            if again is None:
                return findings + _not_reached("R4", "your app did not reconnect in time", skip=1)
            await self._link.until(again, _ended_or_activated, _RESPONSE_S)
            return findings + suite_checks.check_record_dropped(self._link.record(again))
        finally:
            await self._store.remove_record(self._link.client_id)

    async def _declare(self, number: int, payload: ServerActivatePayload) -> bool:
        """
        Send `payload` as a server/activate aiosendspin would not send itself.

        It goes out after aiosendspin's own initial activation. False if the
        connection ended before it went out.
        """
        initial = await self._link.until(
            number, lambda r: r.closed or bool(r.outbound("server/activate")), _RESPONSE_S
        )
        connection = self._link.connection(number)
        if not initial or connection is None or not self._link.is_open(number):
            return False
        before = len(self._link.record(number).outbound("server/activate"))
        connection.send_priority_message(ServerActivateMessage(payload=payload))
        await self._link.until(
            number, lambda r: r.closed or len(r.outbound("server/activate")) > before, _RESPONSE_S
        )
        return len(self._link.record(number).outbound("server/activate")) > before

    async def _set_output_delay(self, number: int) -> int | None:
        """
        Command a random output delay and return it once the client reports it.

        None when the player does not offer set_output_delay or did not report the delay.
        """
        offered = any(
            "set_output_delay" in (_player(f.payload).get("supported_commands") or [])
            for f in self._link.record(number).inbound("client/state")
        )
        client = self._server.get_client(self._link.client_id)
        players = client.roles_by_family("player") if client is not None else []
        if not offered or not players:
            return None
        delay = _DELAY_MIN_MS + secrets.randbelow(_DELAY_SPAN_MS)
        self._prompter.instruct(f"The validator sets your app's output delay to {delay} ms.")
        players[0].set_output_delay(delay)
        echoed = await self._link.until(
            number,
            lambda r: any(
                _player(f.payload).get("output_delay_ms") == delay
                for f in r.inbound("client/state")
            ),
            _RESPONSE_S,
        )
        return delay if echoed else None


_U1 = (
    "The validator checks that your app connected on the Sentinel PSK, as it must to a "
    "server it has never paired with."
)
_U2 = (
    "Turn unpaired access OFF in your app (skip this if it is off by default). The "
    "validator then declares playback anyway; your app must refuse with client/goodbye "
    "'pairing_required'."
)
_U3 = (
    "Turn unpaired access ON in your app. The validator then activates its roles "
    "unpaired; your app must accept them and report client/state."
)
_U4 = (
    "Leave unpaired access off. The validator sends an activation no client may accept: "
    "playback plus pairing_psk pairing on an unpaired session. Your app must close with "
    "client/goodbye 'unauthorized'."
)
_P1 = (
    "Pair your app using its pairing token. The validator re-handshakes onto the token's "
    "pairing PSK, receives the long-term PSK and re-handshakes onto it."
)
_INSTRUCTIONS = {
    "P1": _P1,
    "P2 QR": (
        "The validator starts pairing with a dynamic code in the QR format. Scan the QR your "
        "app shows and paste its text. The validator enters a wrong code first, which your "
        "app must reject, then the right one."
    ),
    "P2": (
        "The validator starts pairing with a dynamic code. Your app shows (or speaks) a "
        "6-digit code: you type it, then a wrong code on purpose, which your app must "
        "reject, then the right code again."
    ),
    "P3": (
        "Make sure your device's pairing window is CLOSED: do not open it yet. The validator "
        "starts pairing with the static code, which your app must hold back until you open "
        "the window."
    ),
    "P4 cancel": (
        "The validator starts pairing with your app's code method, then cancels it. Your app "
        "may show a code briefly; it must drop the attempt and stay connected."
    ),
    "P4 timeout": (
        "Opt-in, slow: the validator starts pairing and enters no code, so your app must give "
        "up after its attempt timeout, about 2 minutes. Answer n to leave this step out."
    ),
    "P3 lockout": (
        "Opt-in, changes your device's state: the validator makes five attempts with wrong "
        "static codes, which must close your device's pairing window, then checks a sixth is "
        "held back. Answer n to leave this step out."
    ),
}
_R1 = "The validator reconnects; your app must match the long-term PSK from pairing."
_R3 = (
    "The validator forgets its pairing with your app but keeps its own identity, then "
    "reconnects. Your app must accept the unpaired handshake; the validator activates "
    "nothing. It restores the pairing afterwards."
)
_R5 = (
    "The validator checks your app keeps its pairing and output delay across a full "
    "restart. It will ask you to quit and restart your app."
)
_R4 = (
    "The validator unpairs (server/unpair). Your app must drop the pairing and close with "
    "client/goodbye 'unpaired'."
)


def _not_reached(step: str, why: str, *, skip: int = 0) -> list[Finding]:
    """Return N/A findings for `step`'s checks after the first `skip`, which already ran."""
    return _not_run(step, Verdict.NOT_APPLICABLE, f"not reached: {why}", skip=skip)


def _not_run(step: str, verdict: Verdict, message: str, *, skip: int = 0) -> list[Finding]:
    """Return `verdict` findings for `step`'s checks after the first `skip`."""
    checks = code_pairing_checks if step in code_pairing.STEP_CHECKS else suite_checks
    return checks.not_run(STEP_CHECKS[step][skip:], verdict, message)


def _ended_or_said(types: set[str]) -> Callable[[ConnectionRecord], bool]:
    """Return a condition: the connection ended, or the client sent one of `types`."""
    return lambda record: record.closed or any(f.inbound and f.type in types for f in record.frames)


def _ended_or_activated(record: ConnectionRecord) -> bool:
    return record.closed or bool(record.outbound("server/activate"))


def _preferred_roles(record: ConnectionRecord) -> list[str]:
    """Return the client's preferred version of each role family, from its client/hello."""
    hello = next(iter(record.inbound("client/hello")), None)
    roles = (hello.payload or {}).get("supported_roles") if hello is not None else None
    preferred: dict[str, str] = {}
    for role in roles if isinstance(roles, list) else []:
        if isinstance(role, str):
            preferred.setdefault(role.partition("@")[0], role)
    return list(preferred.values())


def _pair_methods(record: ConnectionRecord) -> dict[str, Any]:
    hello = next(iter(record.inbound("client/hello")), None)
    methods = (hello.payload or {}).get("supported_pair_methods") if hello is not None else None
    return methods if isinstance(methods, dict) else {}


def _player(payload: dict[str, Any] | None) -> dict[str, Any]:
    player = (payload or {}).get("player")
    return player if isinstance(player, dict) else {}
