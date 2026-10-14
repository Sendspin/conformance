"""
Runs one validation session: serve, let a client connect, and observe it.

The validator is a strict aiosendspin server. Phase 1 activates a client only
through unpaired access and observes the connection's opening: the handshake, the
hello exchange, activation, the initial client/state and time sync.
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

from .discovery import DiscoveredClient, discover_clients
from .prompter import Prompter, Reply
from .recorder import RecordedFrame, Recorder

SERVER_NAME = "Sendspin Validator"
_POLL_S = 0.05
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


@dataclass(frozen=True, slots=True)
class SessionOptions:
    """
    How to run a session.

    `connect_url` skips mDNS and dials that client. `connect_timeout_s` bounds the wait
    for a client to connect.
    """

    port: int
    connect_url: str | None
    connect_timeout_s: float


@dataclass(frozen=True, slots=True)
class SessionResult:
    """What a session observed; `txt_name` is the discovered client's TXT `name`."""

    mode: ConnectionMode
    txt_name: str | None


async def run_session(
    prompter: Prompter,
    recorder: Recorder,
    identity: Identity,
    pairing_store: ServerPairingStore,
    options: SessionOptions,
) -> SessionResult | None:
    """
    Serve one client until its opening exchange is observed, then close.

    Returns None when no client connected. Raises OSError when the port is taken.
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
        await server.start_server(port=options.port, discover_clients=False)
        prompter.instruct(
            f"Advertising _sendspin-server._tcp on port {options.port} {SendspinServer.API_PATH}"
        )
        result = await _connect(server, prompter, recorder, options)
        if result is not None:
            await _Observation(server, prompter, recorder).run()
        return result
    finally:
        await server.close()


async def _connect(
    server: SendspinServer, prompter: Prompter, recorder: Recorder, options: SessionOptions
) -> SessionResult | None:
    """Get a client connected in the mode the user picks; None if none connected."""
    if options.connect_url is not None:
        mode, target = ConnectionMode.SERVER_INITIATED, None
        url: str | None = options.connect_url
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
            mode, target, url = ConnectionMode.CLIENT_INITIATED, None, None
        else:
            mode = ConnectionMode.SERVER_INITIATED
            target = await _pick_client(prompter)
            if target is None:
                return None
            url = target.url

    if url is None:
        prompter.instruct("Waiting for your app to connect…")
    else:
        prompter.instruct(f"Connecting to {url}…")
        try:
            async with asyncio.timeout(options.connect_timeout_s):
                # Declaring playback wins admission over a server that holds the client idle.
                await server.connect_to_client_and_wait(
                    url, connection_reason=ConnectionReason.PLAYBACK
                )
        except (ClientConnectionError, ClientResponseError, TimeoutError) as err:
            prompter.instruct(f"Could not connect to {url}: {err or type(err).__name__}")
            return None
    if not await _wait_for(lambda: bool(recorder.frames), options.connect_timeout_s):
        prompter.instruct(f"No client connected within {options.connect_timeout_s:.0f} s.")
        return None
    return SessionResult(mode=mode, txt_name=target.name if target is not None else None)


async def _pick_client(prompter: Prompter) -> DiscoveredClient | None:
    """Browse for clients until the user picks one; None if they skip."""
    while True:
        prompter.instruct("Browsing for _sendspin._tcp clients…")
        clients = await discover_clients(_DISCOVERY_S)
        for client in clients:
            prompter.instruct(f"Found: {client.label}")
        if not clients:
            prompter.instruct(
                "No client found. Check that your app advertises _sendspin._tcp on this "
                "network, or pass --connect ws://host:port/path."
            )
            answer = await prompter.confirm("Browse again?")
            if answer.reply is Reply.RETRY or (answer.reply is Reply.ANSWERED and answer.value):
                continue
            return None
        answer = await prompter.ask_choice(
            "Which client should the validator connect to?", [c.label for c in clients]
        )
        if answer.reply is Reply.RETRY:
            continue
        if answer.reply is Reply.ANSWERED and answer.value is not None:
            return clients[answer.value]
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
    """Watches the first connection through its opening exchange."""

    def __init__(self, server: SendspinServer, prompter: Prompter, recorder: Recorder) -> None:
        self._prompter = prompter
        self._recorder = recorder
        self._connection = recorder.frames[0].connection
        self._disconnected = False
        server.add_event_listener(self._on_event)

    async def run(self) -> None:
        """Return once the opening exchange is observed, or the connection has ended."""
        await self._watch()
        if goodbye := self._first_in("client/goodbye"):
            reason = (goodbye.payload or {}).get("reason")
            self._prompter.instruct(f"Your app closed the connection: goodbye {reason!r}.")

    async def _watch(self) -> None:
        if not await self._until(lambda: self._first_in("client/hello") is not None):
            return
        hello = self._first_in("client/hello")
        assert hello is not None
        access = (hello.payload or {}).get("unpaired_access")
        if isinstance(access, dict) and access.get("enabled") is False:
            self._prompter.instruct(
                "Your app has unpaired access disabled, so the validator cannot activate "
                "playback without pairing. Pairing support arrives in a later version of "
                "sendspin-validate; only the connection checks run this time."
            )
        if not await self._until(lambda: self._first_out("server/activate") is not None):
            return
        activation = self._first_out("server/activate")
        assert activation is not None
        if (activation.payload or {}).get("active_roles") and not await self._until(
            lambda: self._first_in("client/state") is not None
        ):
            return
        await self._until(lambda: False, timeout_s=_SETTLE_S)

    async def _until(
        self, condition: Callable[[], bool], *, timeout_s: float = _STEP_TIMEOUT_S
    ) -> bool:
        """Wait for `condition`; False if the connection ended or time ran out first."""
        return await _wait_for(lambda: condition() or self._ended(), timeout_s) and condition()

    def _ended(self) -> bool:
        """Whether the observed connection is over, or the server has already redialed."""
        return (
            self._disconnected
            or self._first_out("server/error") is not None
            or any(o.connection == self._connection for o in self._recorder.noncompliance)
            or any(f.connection != self._connection for f in self._recorder.frames)
        )

    def _first_in(self, message_type: str) -> RecordedFrame | None:
        return next(
            (
                f
                for f in self._recorder.frames
                if f.connection == self._connection and f.inbound and f.type == message_type
            ),
            None,
        )

    def _first_out(self, message_type: str) -> RecordedFrame | None:
        return next(
            (
                f
                for f in self._recorder.frames
                if f.connection == self._connection and not f.inbound and f.type == message_type
            ),
            None,
        )

    def _on_event(self, _server: SendspinServer, event: SendspinEvent) -> None:
        if isinstance(event, ClientDisconnectedEvent) and any(
            f.connection == self._connection and f.client_id == event.client_id
            for f in self._recorder.frames
        ):
            self._disconnected = True


async def _wait_for(condition: Callable[[], bool], timeout_s: float) -> bool:
    """Return True once `condition` holds, or False after `timeout_s`."""
    try:
        async with asyncio.timeout(timeout_s):
            while not condition():
                await asyncio.sleep(_POLL_S)
    except TimeoutError:
        return False
    return True
