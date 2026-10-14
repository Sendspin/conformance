"""
Runs one validation session: get a client connected, and observe it.

The validator is a strict aiosendspin server. It activates a client only through
unpaired access and observes the connection's opening: the handshake, the hello
exchange, activation, the initial client/state and time sync.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from aiohttp import ClientConnectionError, ClientResponseError

from conformance._vendor.aiosendspin.models.types import ConnectionReason
from conformance._vendor.aiosendspin.noise import Identity, ServerPairingStore
from conformance._vendor.aiosendspin.server import (
    ClientDisconnectedEvent,
    SendspinEvent,
    SendspinServer,
)

from .checks import ConnectionRecord
from .discovery import DiscoveredClient, discover_clients
from .prompter import Prompter, Reply
from .recorder import Recorder

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
    that client. `connect_timeout_s` bounds the wait for a client to connect.
    """

    port: int
    connect_url: str | None
    connect_timeout_s: float


@dataclass(frozen=True, slots=True)
class SessionResult:
    """
    What a session observed.

    `connection` is the number of the observed connection; frames on any other belong
    to other clients and are not judged. `txt_name` is the discovered client's TXT
    `name`, when it was discovered over mDNS.
    """

    mode: ConnectionMode
    connection: int
    txt_name: str | None


async def run_session(
    prompter: Prompter,
    recorder: Recorder,
    identity: Identity,
    pairing_store: ServerPairingStore,
    options: SessionOptions,
) -> SessionResult | None:
    """
    Get one client connected, observe its opening exchange, then close.

    Returns None when no client connected. Raises PortUnavailableError when the
    validator cannot listen on `options.port`.
    """
    server = SendspinServer(
        loop=asyncio.get_running_loop(),
        identity=identity,
        server_name=SERVER_NAME,
        pairing_store=pairing_store,
        allow_noncompliant_clients=False,
        frame_observer=recorder.observe_frame,
        noncompliance_observer=recorder.observe_noncompliance,
    )
    try:
        result = await _connect(server, prompter, recorder, options)
        if result is not None:
            await _Observation(server, prompter, recorder, result.connection).run()
        return result
    finally:
        await server.close()


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
                "Your app has unpaired access disabled, so the validator cannot activate "
                "playback without pairing. Pairing support arrives in a later version of "
                "sendspin-validate; only the connection checks run this time."
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
