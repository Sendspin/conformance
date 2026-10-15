"""
Runs one validation session: get a client connected, observe it, then run the suites.

The validator is a strict aiosendspin server. It observes the first connection's
opening: the handshake, the hello exchange, activation, the initial client/state and
time sync. The selected suites then drive the same client through further
connections, unpaired and paired.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from aiohttp import ClientConnectionError, ClientResponseError

from conformance._vendor.aiosendspin.models.types import ConnectionReason
from conformance._vendor.aiosendspin.noise import Identity
from conformance._vendor.aiosendspin.server import (
    ClientConnectedEvent,
    ClientCredentialMismatchEvent,
    ClientDisconnectedEvent,
    SendspinEvent,
    SendspinServer,
)

from .checks import ConnectionRecord
from .code_pairing import spoken_language
from .discovery import DiscoveredClient, discover_clients
from .findings import Finding
from .prompter import Prompter, Reply
from .recorder import EventKind, Recorder
from .storage import AdmitUnpairedStore
from .suites import Link, Suite, SuiteOptions, SuiteRunner

SERVER_NAME = "Sendspin Validator"
# How long to browse for clients before listing them.
_DISCOVERY_S = 3.0
# How long to keep watching once the opening exchange is done, for client/time.
_SETTLE_S = 3.0
# How long the client gets for each step of the opening exchange.
_STEP_TIMEOUT_S = 30.0


class ConnectionMode(StrEnum):
    """Which side opens the WebSocket."""

    CLIENT_INITIATED = "client-initiated"
    SERVER_INITIATED = "server-initiated"


class PortUnavailableError(OSError):
    """Raised when the validator cannot listen on the requested port."""


@dataclass(frozen=True, slots=True)
class SessionOptions:
    """
    How to run a session.

    `port` is listened on in client-initiated mode. `connect_url` skips mDNS and dials
    that client. `connect_timeout_s` bounds the wait for a client to connect, and for
    it to reconnect during a suite. `suites` run after the opening exchange;
    `fault_injection` allows their steps that send what no client may accept. `seed`
    drives every randomized challenge, among them the language server/hello names.
    """

    port: int
    connect_url: str | None
    connect_timeout_s: float
    suites: frozenset[Suite] = frozenset()
    fault_injection: bool = False
    seed: int = 0


@dataclass(frozen=True, slots=True)
class SessionResult:
    """
    What a session observed.

    `connection` is the number of the observed connection, and `later_connections`
    those the suites opened to the same client; frames on any other belong to other
    clients and are not judged. `txt_name` is the discovered client's TXT `name`, when
    it was discovered over mDNS. `url` is the client's, in server-initiated mode.
    `findings` are the suites' verdicts.
    """

    mode: ConnectionMode
    connection: int
    txt_name: str | None
    url: str | None = None
    later_connections: tuple[int, ...] = ()
    findings: tuple[Finding, ...] = ()


async def run_session(
    prompter: Prompter,
    recorder: Recorder,
    identity: Identity,
    pairing_store: AdmitUnpairedStore,
    options: SessionOptions,
) -> SessionResult | None:
    """
    Get one client connected, observe its opening exchange, run the suites, then close.

    Returns None when no client connected. Raises PortUnavailableError when the
    validator cannot listen on `options.port`.
    """
    server = SendspinServer(
        loop=asyncio.get_running_loop(),
        identity=identity,
        server_name=SERVER_NAME,
        # Not English, so a client that speaks the pairing code shows it follows the server.
        languages=[spoken_language(options.seed)],
        pairing_store=pairing_store,
        allow_noncompliant_clients=False,
        frame_observer=recorder.observe_frame,
        noncompliance_observer=recorder.observe_noncompliance,
    )
    server.add_event_listener(_ConnectionEvents(recorder).on_event)
    try:
        result = await _connect(server, prompter, recorder, options)
        if result is None:
            return None
        await _Observation(server, prompter, recorder, result.connection).run()
        if not options.suites:
            return result
        return await _run_suites(server, prompter, recorder, pairing_store, options, result)
    finally:
        await server.close()


async def _run_suites(
    server: SendspinServer,
    prompter: Prompter,
    recorder: Recorder,
    pairing_store: AdmitUnpairedStore,
    options: SessionOptions,
    result: SessionResult,
) -> SessionResult:
    """Run the selected suites on the observed client, if it completed its opening."""
    opening = ConnectionRecord.of(result.connection, recorder.frames, recorder.noncompliance)
    init = next(iter(opening.inbound("client/init", phase="cleartext")), None)
    client_id = (init.payload or {}).get("client_id") if init is not None else None
    if not isinstance(client_id, str) or not any(e.client_id == client_id for e in recorder.events):
        prompter.instruct("The suites need a client that completes the opening; none ran.")
        return result
    link = Link(
        server,
        prompter,
        recorder,
        client_id=client_id,
        url=result.url,
        timeout_s=options.connect_timeout_s,
    )
    suite_options = SuiteOptions(
        suites=options.suites, fault_injection=options.fault_injection, seed=options.seed
    )
    findings = await SuiteRunner(
        server, prompter, recorder, pairing_store, link, suite_options
    ).run()
    return replace(
        result,
        later_connections=tuple(link.connections_since(result.connection)),
        findings=tuple(findings),
    )


async def _connect(
    server: SendspinServer, prompter: Prompter, recorder: Recorder, options: SessionOptions
) -> SessionResult | None:
    """Get a client connected in the mode the user picks; None if none connected."""
    target: DiscoveredClient | None = None
    if options.connect_url is not None:
        url = options.connect_url
    else:
        choice = await _choose(
            prompter,
            "How will your app connect?",
            [
                "My app connects to the validator (client-initiated)",
                "The validator connects to my app (server-initiated)",
            ],
        )
        if choice is None:
            return None
        if choice == 0:
            return await _accept(server, prompter, recorder, options)
        target = await _pick_client(prompter)
        if target is None:
            return None
        url = target.url

    # The validator only dials here and does not listen, so no other client can reach it.
    prompter.instruct(
        f"Connecting to {url}… If another server is using this client idle, the validator "
        "takes it over for the run."
    )
    try:
        async with asyncio.timeout(options.connect_timeout_s):
            # Declaring playback wins admission over a server that holds the client idle.
            await server.connect_to_client_and_wait(
                url, connection_reason=ConnectionReason.PLAYBACK
            )
            await recorder.wait_until(lambda: bool(recorder.frames))
    except (ClientConnectionError, ClientResponseError, TimeoutError) as err:
        prompter.instruct(f"Could not connect to {url}: {err or type(err).__name__}")
        return None
    return SessionResult(
        mode=ConnectionMode.SERVER_INITIATED,
        connection=recorder.frames[0].connection,
        txt_name=target.name if target is not None else None,
        url=url,
    )


async def _accept(
    server: SendspinServer, prompter: Prompter, recorder: Recorder, options: SessionOptions
) -> SessionResult | None:
    """Listen and wait for a client to connect; None if none did in time."""
    try:
        await server.start_server(port=options.port, discover_clients=False)
    except OSError as err:
        raise PortUnavailableError(err.errno, f"cannot listen on port {options.port}") from err
    prompter.instruct(
        f"Listening on port {options.port} {SendspinServer.API_PATH}, announced as "
        "_sendspin-server._tcp over mDNS."
    )
    prompter.instruct("Waiting for your app to connect…")
    try:
        async with asyncio.timeout(options.connect_timeout_s):
            await recorder.wait_until(lambda: bool(recorder.frames))
    except TimeoutError:
        prompter.instruct(f"No client connected within {options.connect_timeout_s:.0f} s.")
        return None
    return SessionResult(
        mode=ConnectionMode.CLIENT_INITIATED,
        connection=recorder.frames[0].connection,
        txt_name=None,
    )


async def _pick_client(prompter: Prompter) -> DiscoveredClient | None:
    """Browse for clients until the user picks one; None if they skip."""
    while True:
        prompter.instruct("Browsing for _sendspin._tcp clients…")
        discovery = await discover_clients(_DISCOVERY_S)
        for client in discovery.clients:
            prompter.instruct(f"Found: {client.label}")
        for unusable in discovery.unusable:
            prompter.instruct(f"Found but cannot connect: {unusable}")
        if not discovery.clients:
            prompter.instruct(
                "No usable client found. Check that your app advertises _sendspin._tcp with "
                "a TXT path on this network, or pass --connect ws://host:port/path."
            )
            answer = await prompter.confirm("Browse again?")
            if answer.reply is Reply.RETRY or (answer.reply is Reply.ANSWERED and answer.value):
                continue
            return None
        answer = await prompter.ask_choice(
            "Which client should the validator connect to?",
            [c.label for c in discovery.clients],
        )
        if answer.reply is Reply.RETRY:
            continue
        if answer.reply is Reply.ANSWERED and answer.value is not None:
            return discovery.clients[answer.value]
        return None


async def _choose(prompter: Prompter, question: str, choices: list[str]) -> int | None:
    """Ask until the user picks a choice, asking again on retry; None if they skip."""
    while True:
        answer = await prompter.ask_choice(question, choices)
        if answer.reply is Reply.ANSWERED:
            return answer.value
        if answer.reply is not Reply.RETRY:
            return None


class _ConnectionEvents:
    """
    Records the connection events aiosendspin reports and no frame shows.

    For each client it keeps the connection attached last, which a credential
    mismatch or a disconnect refers to.
    """

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder
        self._attached: dict[str, int] = {}

    def on_event(self, server: SendspinServer, event: SendspinEvent) -> None:
        """Record which PSK keyed an attached connection, a credential mismatch, or a close."""
        match event:
            case ClientConnectedEvent(client_id=client_id):
                client = server.get_client(client_id)
                connection = client.connection if client is not None else None
                if (
                    connection is None
                    or connection.connection_number is None
                    or connection.psk_category is None
                ):
                    return
                self._attached[client_id] = connection.connection_number
                self._recorder.observe_event(
                    connection.connection_number,
                    client_id,
                    EventKind.KEYED,
                    psk_category=connection.psk_category.value,
                )
            case ClientCredentialMismatchEvent(client_id=client_id) if client_id in self._attached:
                self._recorder.observe_event(
                    self._attached[client_id], client_id, EventKind.CREDENTIAL_MISMATCH
                )
            case ClientDisconnectedEvent(client_id=client_id) if client_id in self._attached:
                self._recorder.observe_event(
                    self._attached.pop(client_id), client_id, EventKind.CLOSED
                )


class _Observation:
    """Watches one connection through its opening exchange."""

    def __init__(
        self, server: SendspinServer, prompter: Prompter, recorder: Recorder, connection: int
    ) -> None:
        self._prompter = prompter
        self._recorder = recorder
        self._connection = connection
        self._disconnected = False
        server.add_event_listener(self._on_event)

    async def run(self) -> None:
        """Return once the opening exchange is observed, or the connection has ended."""
        await self._watch()
        if goodbye := next(iter(self._record().inbound("client/goodbye")), None):
            reason = (goodbye.payload or {}).get("reason")
            self._prompter.instruct(f"Your app closed the connection: goodbye {reason!r}.")

    async def _watch(self) -> None:
        if not await self._until(lambda record: bool(record.inbound("client/hello"))):
            return
        hello = self._record().inbound("client/hello")[0]
        access = (hello.payload or {}).get("unpaired_access")
        if isinstance(access, dict) and access.get("enabled") is False:
            self._prompter.instruct(
                "Your app has unpaired access disabled, so the validator activates no roles "
                "until it is paired."
            )
        if not await self._until(lambda record: bool(record.outbound("server/activate"))):
            return
        activation = self._record().outbound("server/activate")[0]
        if (activation.payload or {}).get("active_roles") and not await self._until(
            lambda record: bool(record.inbound("client/state"))
        ):
            return
        await self._until(lambda _record: False, timeout_s=_SETTLE_S)

    async def _until(
        self,
        condition: Callable[[ConnectionRecord], bool],
        *,
        timeout_s: float = _STEP_TIMEOUT_S,
    ) -> bool:
        """Wait for `condition`; False if the connection ended or time ran out first."""
        try:
            async with asyncio.timeout(timeout_s):
                await self._recorder.wait_until(lambda: condition(self._record()) or self._ended())
        except TimeoutError:
            return False
        return condition(self._record())

    def _record(self) -> ConnectionRecord:
        return ConnectionRecord.of(
            self._connection, self._recorder.frames, self._recorder.noncompliance
        )

    def _ended(self) -> bool:
        """Whether the connection is over: closed, rejected, or answered with server/error."""
        record = self._record()
        return (
            self._disconnected
            or record.rejected
            or bool(record.outbound("server/error", phase="cleartext"))
        )

    def _on_event(self, _server: SendspinServer, event: SendspinEvent) -> None:
        if isinstance(event, ClientDisconnectedEvent) and any(
            f.client_id == event.client_id for f in self._record().frames
        ):
            self._disconnected = True
            self._recorder.notify()
