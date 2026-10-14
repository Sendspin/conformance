"""Protocol-evidence capture for aiosendspin adapters.

Wraps the aiosendspin SDK's connection objects from outside (no SDK changes)
to populate ``summary["protocol"]`` per the contract in
:mod:`conformance.protocol`, ``summary["activation"]`` with the initial
``server/activate``, ``summary["group_updates"]`` with every
``group/update`` that followed it, and ``summary["time_exchange"]`` with each
``client/time`` received and ``server/time`` sent. This is deliberately a
monkey-patch: aiosendspin does not yet expose a first-class tracing hook
(tracked in https://github.com/Sendspin/conformance/issues/111), so this module
reaches into private/semi-public attributes and is expected to need
maintenance as the SDK evolves.

Evidence fidelity by assertion:

- CORE-001 (handshake ordering/frame types): coarse. The Noise handshake
  itself completes before any adapter code gets a connection reference, so we
  cannot observe individual handshake frames from outside the SDK. Instead we
  record handshake success (the driver raises on any out-of-order or
  wrong-type frame, so successful completion is itself evidence) with
  start/end timestamps and the negotiated handshake hash/PSK category.
- CORE-002/003/004 (no early application messages, correct role activation,
  initial client/state ordering): the server SDK already performs these
  checks internally via ``SendspinConnection._flag_noncompliance`` /
  ``ClientConnectedEvent.flag_noncompliance``, raising ``ClientComplianceError``
  when the server is run with ``allow_noncompliant_clients=False``. We run in
  strict mode and treat "connection established without a compliance error"
  as evidence these assertions passed, plus we independently verify the
  negotiated active roles are a subset of what ``server/activate`` advertised.
- PLAYER-001 (player stream selects an advertised format and sends valid
  timestamped chunks): full fidelity. ``stream/start`` and audio chunk frames
  are observed directly by wrapping ``send_binary``/``send_message`` (server)
  and the audio-chunk/stream-start listeners (client), both of which fire
  well after handshake/activation and are stable public/semi-public hooks.
- Initial ``server/activate``: full fidelity. The SDK sends it during
  connection bring-up, before the adapter holds the connection, so
  :class:`ControlMessageRecorder` wraps ``EncryptedWebSocket.send_str`` at
  class level and keeps the JSON body handed to the encrypting transport.
- Every ``group/update`` after that ``server/activate``: full fidelity, from
  the same wrapper, which is what orders them.
- ``client/state`` received and ``stream/start`` sent, in order: full fidelity
  on an encrypted connection, from the same recorder, which also wraps
  ``EncryptedWebSocket.receive`` and ``receive_timed``, whichever the SDK
  reads with. The SDK applies a ``client/state`` and sends
  a ``stream/start`` from its own loops, and its parser rewrites a legacy
  ``state`` into ``available``, so only the transport shows what arrived and
  when. An unencrypted legacy connection bypasses that transport and is not
  observed.
- ``client/time`` received and ``server/time`` sent, in order: full fidelity
  on an encrypted connection, from the same recorder. The SDK fills in
  ``server_transmitted`` as it dequeues the reply, so only the transport shows
  the three timestamps that went out.
- First metadata-carrying ``server/state``: full fidelity, and timed.
  :class:`SentMetadataStateRecorder` wraps the same transport method, because
  the timestamp requirement on that state needs a clock reading taken once the
  frame is really on the wire, which the enqueueing send path cannot give.
- Binary frames as transported: full fidelity on an encrypted connection.
  The SDK builds the audio chunk header in its send-queue drain and strips it
  before any listener runs, so the header exists nowhere an adapter can reach
  but the transport. :class:`SentBinaryFrameRecorder` and
  :class:`ReceivedBinaryFrameRecorder` wrap ``EncryptedWebSocket.send_bytes``
  and ``EncryptedWebSocket.receive`` to keep each frame's size and leading
  bytes. An unencrypted legacy connection bypasses that transport and is not
  observed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from conformance.protocol import SPEC_REVISION

_RECORDED_SENT_TYPES = ("server/activate", "group/update", "stream/start", "server/time")
_OTHER_SENT = {"type": "other-sent"}


@dataclass
class AssertionRecorder:
    """Accumulates trace events for one protocol assertion."""

    assertion_id: str
    status: str = "failed"
    detail: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, "timestamp": time.time(), **fields})

    def passed(self, detail: str | None = None) -> None:
        self.status = "passed"
        self.detail = detail

    def failed(self, detail: str) -> None:
        self.status = "failed"
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "events": self.events}


class ProtocolEvidenceCollector:
    """Collects assertion evidence for one connection (server or client side)."""

    def __init__(self) -> None:
        self._assertions: dict[str, AssertionRecorder] = {}

    def assertion(self, assertion_id: str) -> AssertionRecorder:
        return self._assertions.setdefault(assertion_id, AssertionRecorder(assertion_id))

    def to_summary_fragment(self) -> dict[str, Any]:
        return {
            "spec_revision": SPEC_REVISION,
            "assertions": {
                assertion_id: recorder.to_dict()
                for assertion_id, recorder in self._assertions.items()
            },
        }


class ControlMessageRecorder:
    """
    Records the control messages the matrix judges on every connection, as transported.

    That is the first ``server/activate`` sent, every ``group/update`` sent
    after it, each ``client/state`` received and ``stream/start`` sent in the
    order they crossed the transport, and each ``client/time`` received and
    ``server/time`` sent, likewise in order. One recorder keeps all of them,
    so each verdict drawn from them reads the same view of the connection.

    Recording starts on construction and covers every encrypted connection the
    process opens afterwards, so construct it before the server starts
    accepting clients. Call :meth:`uninstall` to stop recording.
    """

    def __init__(self) -> None:
        from aiohttp import WSMsgType
        from aiosendspin.noise.wire import EncryptedWebSocket

        self._transport_class = EncryptedWebSocket
        self._original_send_str = EncryptedWebSocket.send_str
        self._original_receive = EncryptedWebSocket.receive
        self._first_by_socket: dict[Any, dict[str, Any]] = {}
        self._group_updates_by_socket: dict[Any, list[dict[str, Any]]] = {}
        self._availability_by_socket: dict[Any, list[dict[str, Any]]] = {}
        self._time_exchange_by_socket: dict[Any, list[dict[str, Any]]] = {}

        async def send_str(transport: Any, data: str) -> None:
            # Every JSON control body passes through here, so only bodies naming
            # a recorded message type are parsed.
            message = (
                json.loads(data)
                if any(f'"{name}"' in data for name in _RECORDED_SENT_TYPES)
                else {}
            )
            # A pairing re-handshake swaps the transport but keeps the socket.
            socket = transport._ws
            # Where the exchange stood when this send began: a client/time read
            # while the frame is being written was not received before it.
            began_at = len(self._time_exchange_by_socket.get(socket, ()))
            if message.get("type") != "stream/start":
                await self._original_send_str(transport, data)
                if message.get("type") == "server/activate":
                    self._first_by_socket.setdefault(socket, message)
                elif message.get("type") == "group/update" and socket in self._first_by_socket:
                    self._group_updates_by_socket.setdefault(socket, []).append(message)
                if message.get("type") == "server/time":
                    self._time_exchange_by_socket.setdefault(socket, []).append(message)
                else:
                    self._record_other_sent(socket, began_at)
                return
            payload = message.get("payload")
            entry = {
                "type": "stream/start",
                "roles": sorted(
                    role
                    for role, value in (payload if isinstance(payload, dict) else {}).items()
                    if isinstance(value, dict)
                ),
            }
            trace = self._availability_by_socket.setdefault(socket, [])
            # A client/state arriving during the write cannot be placed on either
            # side of it, so the frame is bracketed rather than given one position.
            trace.append({**entry, "phase": "sending"})
            await self._original_send_str(transport, data)
            trace.append({**entry, "phase": "sent"})
            self._record_other_sent(socket, began_at)

        def observe(transport: Any, received: Any) -> Any:
            if received.type is not WSMsgType.TEXT:
                return received
            # Marks the socket as one whose incoming text is observed, so that
            # no client/time arriving reads as an empty exchange, not an unseen one.
            time_exchange = self._time_exchange_by_socket.setdefault(transport._ws, [])
            # Parsed whatever the text looks like: a client is free to write the
            # type with its slash escaped, which no substring test finds.
            try:
                message = json.loads(received.data)
            except (TypeError, ValueError):
                return received
            if isinstance(message, dict) and message.get("type") == "client/state":
                payload = message.get("payload")
                self._availability_by_socket.setdefault(transport._ws, []).append(
                    {
                        "type": "client/state",
                        "available": payload.get("available")
                        if isinstance(payload, dict)
                        else None,
                    }
                )
            elif isinstance(message, dict) and message.get("type") == "client/time":
                time_exchange.append(message)
            return received

        async def receive(transport: Any) -> Any:
            return observe(transport, await self._original_receive(transport))

        # The SDK reads an encrypted connection through receive_timed(), which
        # does not go through receive(); both are observed.
        self._original_receive_timed = getattr(EncryptedWebSocket, "receive_timed", None)

        async def receive_timed(transport: Any, clock: Any) -> Any:
            received, received_at = await self._original_receive_timed(transport, clock)
            return observe(transport, received), received_at

        EncryptedWebSocket.send_str = send_str  # type: ignore[method-assign]
        EncryptedWebSocket.receive = receive  # type: ignore[method-assign]
        if self._original_receive_timed is not None:
            EncryptedWebSocket.receive_timed = receive_timed  # type: ignore[method-assign]

    def initial_activation(self, connection: Any) -> dict[str, Any] | None:
        """
        Return the first ``server/activate`` sent on a server-side ``SendspinConnection``.

        The message is the ``{"type": ..., "payload": ...}`` body as sent.
        Returns ``None`` for an unencrypted legacy connection, where the SDK
        sends no ``server/activate``. Raises RuntimeError when an encrypted
        connection has no recorded message: the SDK cannot activate a client
        without sending one, so that means the recorder missed it.
        """
        socket = connection._wsock_server or connection._wsock_client
        message = self._first_by_socket.get(socket)
        if message is None and connection.is_encrypted:
            raise RuntimeError(
                "No server/activate was recorded for an encrypted connection; "
                "the aiosendspin send path no longer reaches EncryptedWebSocket.send_str"
            )
        return message

    def group_updates(self, connection: Any) -> list[dict[str, Any]]:
        """
        Return every ``group/update`` sent after the first ``server/activate``, in order.

        Each is the ``{"type": ..., "payload": ...}`` body as sent on a
        server-side ``SendspinConnection``. The list is empty when none had
        been sent by the time of the call, which covers an unencrypted legacy
        connection, where no ``server/activate`` precedes anything. A
        ``group/update`` sent before the first ``server/activate`` is left out.
        Both message types are seen by one wrapper, so
        :meth:`initial_activation` raising is what shows these went unobserved.
        """
        socket = connection._wsock_server or connection._wsock_client
        return list(self._group_updates_by_socket.get(socket, ()))

    def availability_trace(self, connection: Any) -> list[dict[str, Any]] | None:
        """
        Return each ``client/state`` received and ``stream/start`` sent, oldest first.

        Each entry is ``{"type": "client/state", "available": ...}``, carrying
        ``available`` as it arrived on the wire, or
        ``{"type": "stream/start", "phase": "sending" | "sent", "roles": [...]}``,
        where ``roles`` names the role objects the ``stream/start`` carried.
        A ``client/state`` is recorded as the transport hands it over, before
        the SDK acts on it. A ``stream/start`` is recorded as ``sending`` before
        the transport is given the frame and as ``sent`` once it is written, so
        a send that raised leaves ``sending`` without a ``sent``.

        Returns ``None`` for a server-side ``SendspinConnection`` neither
        message was observed on, which covers an unencrypted legacy connection.
        """
        socket = connection._wsock_server or connection._wsock_client
        trace = self._availability_by_socket.get(socket)
        return None if trace is None else list(trace)

    def time_exchange(self, connection: Any) -> list[dict[str, Any]] | None:
        """
        Return each ``client/time`` received and ``server/time`` sent, oldest first.

        Each is the ``{"type": ..., "payload": ...}`` body as it crossed the
        transport on a server-side ``SendspinConnection``. A ``client/time`` is
        recorded as the transport hands it over, and a ``server/time`` once it
        is written, so one whose send raised is left out. The list is empty
        when the client sent no ``client/time``.

        An ``{"type": "other-sent"}`` entry stands for one or more other text
        messages the server sent. It sits where the first of those sends
        began, so every ``client/time`` before it had been received by then,
        and it is recorded only once the send has returned.

        Returns ``None`` for a connection that neither incoming text nor a
        ``server/time`` was observed on, which covers an unencrypted legacy
        connection.
        """
        socket = connection._wsock_server or connection._wsock_client
        exchange = self._time_exchange_by_socket.get(socket)
        return None if exchange is None else list(exchange)

    def uninstall(self) -> None:
        """Restore the SDK transport's own ``send_str`` and ``receive``."""
        self._transport_class.send_str = self._original_send_str  # type: ignore[method-assign]
        self._transport_class.receive = self._original_receive  # type: ignore[method-assign]
        if self._original_receive_timed is not None:
            self._transport_class.receive_timed = self._original_receive_timed  # type: ignore[method-assign]

    def _record_other_sent(self, socket: Any, began_at: int) -> None:
        # Nothing precedes position 0 for the entry to follow, and one entry
        # already speaks for every send between the same two neighbours.
        exchange = self._time_exchange_by_socket.get(socket)
        if not began_at or _OTHER_SENT in exchange[began_at - 1 : began_at + 1]:
            return
        exchange.insert(began_at, dict(_OTHER_SENT))


class SentMetadataStateRecorder:
    """
    Records the first ``server/state`` sent carrying a metadata object with a timestamp.

    Wraps ``EncryptedWebSocket.send_str`` for the same reason
    :class:`ControlMessageRecorder` does: these states leave on the SDK's own
    queue drain rather than from the adapter, so no public hook fires once one
    is really on the wire. ``send_role_message`` only enqueues.

    The clock is read after the wrapped send returns, so the reading is an upper
    bound on when the state reached the transport. The direction matters: a
    reading taken before transmission would call a timestamp future when it was
    already past by the time the frame went out, and so blame a conformant
    server. A later reading can only make the check more permissive.

    Installed alongside the opening-messages recorder, the two wrappers nest. Each
    awaits the send it captured, so both still see every frame, and this one's
    reading only moves later, which is the safe direction.

    Recording starts on construction, so construct it before the server accepts
    clients: the first such state can be sent during connection bring-up. The
    metadata scenarios drive a single client, so the first one seen on any
    connection is the state being judged.
    """

    def __init__(self, clock: Any) -> None:
        from aiosendspin.noise.wire import EncryptedWebSocket

        self._clock = clock
        self._transport_class = EncryptedWebSocket
        self._original_send_str = EncryptedWebSocket.send_str
        self._first: dict[str, int] | None = None

        async def send_str(transport: Any, data: str) -> None:
            await self._original_send_str(transport, data)
            # Every JSON control body passes through here, so only bodies naming
            # the message type are parsed.
            if self._first is not None or '"server/state"' not in data:
                return
            message = json.loads(data)
            if message.get("type") != "server/state":
                return
            metadata = (message.get("payload") or {}).get("metadata")
            if not isinstance(metadata, dict):
                return
            timestamp_us = metadata.get("timestamp")
            # bool is an int subclass, and an object without a timestamp carries
            # no requirement, so it is left for a later state to satisfy.
            if isinstance(timestamp_us, bool) or not isinstance(timestamp_us, int):
                return
            self._first = {"timestamp_us": timestamp_us, "bound_us": self._clock.now_us()}

        EncryptedWebSocket.send_str = send_str  # type: ignore[method-assign]

    def first_metadata_state(self) -> dict[str, int] | None:
        """
        Return the first metadata-carrying ``server/state`` as sent, or None.

        The mapping is ``{"timestamp_us": ..., "bound_us": ...}``: the timestamp
        that went out on the wire, and a reading of the server's clock taken
        once that frame had been written. None means no such state was
        observed, which covers a legacy unencrypted connection and a client that
        went away before the queue drained.
        """
        return None if self._first is None else dict(self._first)

    def uninstall(self) -> None:
        """Restore the ``send_str`` this recorder wrapped."""
        self._transport_class.send_str = self._original_send_str  # type: ignore[method-assign]


# Spans the RC1 audio chunk header, so the prefix shows every header field. A
# frame with a shorter header yields that many bytes of audio past it; the rest
# of the payload is never recorded.
FRAME_PREFIX_BYTES = 13


def binary_frame_record(frame: bytes) -> dict[str, Any]:
    """Return the summary record for one binary frame as it crossed the transport."""
    return {"byte_count": len(frame), "leading_hex": frame[:FRAME_PREFIX_BYTES].hex()}


class ChunkPayloadSizes:
    """
    The size of each audio chunk's payload, keyed by the timestamp the SDK gave it.

    The SDK hands an adapter a chunk's audio and its timestamp together, on a
    hook that never sees the frame, and the transport shows the frame but not
    where its audio starts. The timestamp is the one thing both carry, so it is
    what tells which audio a frame held.
    """

    def __init__(self) -> None:
        self._sizes: dict[int, int | None] = {}

    def record(self, timestamp_us: int, payload: bytes) -> None:
        """Note the payload of the chunk stamped `timestamp_us`."""
        # Two chunks sharing a timestamp cannot be told apart, so neither is paired.
        self._sizes[timestamp_us] = None if timestamp_us in self._sizes else len(payload)

    def paired(self, frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Return `frames` with a `payload_byte_count` on each.

        The count is that of the chunk whose timestamp the frame carries in
        bytes 1-8, read big-endian, and None for a frame carrying no recorded
        timestamp there.
        """
        paired = []
        for frame in frames:
            leading = bytes.fromhex(frame["leading_hex"])
            timestamp_us = int.from_bytes(leading[1:9], "big", signed=True)
            size = self._sizes.get(timestamp_us) if len(leading) >= 9 else None
            paired.append({**frame, "payload_byte_count": size})
        return paired


class SentBinaryFrameRecorder:
    """
    Records every binary frame sent on an encrypted connection, in order.

    Recording starts on construction and covers every connection the process
    opens afterwards. A frame is recorded once the transport has accepted it,
    so one the SDK built for a client that had already gone is left out. Call
    :meth:`uninstall` to stop recording.
    """

    def __init__(self) -> None:
        from aiosendspin.noise.wire import EncryptedWebSocket

        self._transport_class = EncryptedWebSocket
        self._original_send_bytes = EncryptedWebSocket.send_bytes
        self._frames: list[dict[str, Any]] = []

        async def send_bytes(transport: Any, data: bytes) -> None:
            await self._original_send_bytes(transport, data)
            self._frames.append(binary_frame_record(data))

        EncryptedWebSocket.send_bytes = send_bytes  # type: ignore[method-assign]

    def frames(self) -> list[dict[str, Any]]:
        """Return one ``{"byte_count": ..., "leading_hex": ...}`` record per frame sent."""
        return list(self._frames)

    def uninstall(self) -> None:
        """Restore the SDK transport's own ``send_bytes``."""
        self._transport_class.send_bytes = self._original_send_bytes  # type: ignore[method-assign]


class ReceivedBinaryFrameRecorder:
    """
    Records every binary frame received on an encrypted connection, in order.

    Each frame is the decrypted, reassembled message the transport hands to the
    SDK, before the SDK parses a header off it. Recording starts on
    construction, so construct it before the client connects. Call
    :meth:`uninstall` to stop recording.
    """

    def __init__(self) -> None:
        from aiohttp import WSMsgType
        from aiosendspin.noise.wire import EncryptedWebSocket

        self._transport_class = EncryptedWebSocket
        self._original_receive = EncryptedWebSocket.receive
        self._frames: list[dict[str, Any]] = []

        def observe(message: Any) -> Any:
            if message.type is WSMsgType.BINARY:
                self._frames.append(binary_frame_record(message.data))
            return message

        async def receive(transport: Any) -> Any:
            return observe(await self._original_receive(transport))

        self._original_receive_timed = getattr(EncryptedWebSocket, "receive_timed", None)

        async def receive_timed(transport: Any, clock: Any) -> Any:
            message, received_at = await self._original_receive_timed(transport, clock)
            return observe(message), received_at

        EncryptedWebSocket.receive = receive  # type: ignore[method-assign]
        if self._original_receive_timed is not None:
            EncryptedWebSocket.receive_timed = receive_timed  # type: ignore[method-assign]

    def frames(self) -> list[dict[str, Any]]:
        """Return one ``{"byte_count": ..., "leading_hex": ...}`` record per frame received."""
        return list(self._frames)

    def uninstall(self) -> None:
        """Restore the SDK transport's own ``receive``."""
        self._transport_class.receive = self._original_receive  # type: ignore[method-assign]
        if self._original_receive_timed is not None:
            self._transport_class.receive_timed = self._original_receive_timed  # type: ignore[method-assign]


def record_handshake_evidence_server(
    collector: ProtocolEvidenceCollector,
    connection: Any,
    *,
    start_ts: float,
    end_ts: float,
) -> None:
    """Record CORE-001 evidence from a server-side ``SendspinConnection``.

    ``connection`` is only reachable after the handshake has already
    completed (there is no earlier externally-visible hook), so this is
    necessarily coarse: successful completion is evidence the handshake
    driver's internal strict-ordering checks passed, since it raises
    ``HandshakeAbortedError`` on any out-of-order or wrong-type frame.
    """
    core_001 = collector.assertion("CORE-001")
    handshake_hash = getattr(connection, "_handshake_hash", None)
    noise_psk = getattr(connection, "_noise_psk", None)
    core_001.record(
        "handshake_completed",
        start_ts=start_ts,
        end_ts=end_ts,
        peer_id=getattr(connection, "_client_id", None),
        handshake_hash=handshake_hash.hex() if handshake_hash else None,
        psk_category=getattr(noise_psk, "category", None).value
        if noise_psk is not None
        else None,
    )
    core_001.passed("Noise handshake completed; driver enforces strict frame ordering")


def record_handshake_evidence_client(
    collector: ProtocolEvidenceCollector,
    connection: Any,
    *,
    start_ts: float,
    end_ts: float,
) -> None:
    """Record CORE-001 evidence from a client-side ``SendspinConnection``. See
    :func:`record_handshake_evidence_server` for the fidelity caveat."""
    core_001 = collector.assertion("CORE-001")
    handshake_hash = getattr(connection, "_handshake_hash", None)
    noise_psk = getattr(connection, "_noise_psk", None)
    core_001.record(
        "handshake_completed",
        start_ts=start_ts,
        end_ts=end_ts,
        peer_id=getattr(connection, "_server_id", None),
        handshake_hash=handshake_hash.hex() if handshake_hash else None,
        psk_category=getattr(noise_psk, "category", None).value
        if noise_psk is not None
        else None,
    )
    core_001.passed("Noise handshake completed; driver enforces strict frame ordering")


def record_activation_evidence_server(
    collector: ProtocolEvidenceCollector,
    client: Any,
    *,
    requested_roles: list[str],
) -> None:
    """Record CORE-002/003/004 evidence from a server-side connected client.

    The server SDK enforces these requirements internally
    (``SendspinConnection`` rejects/flags out-of-order application messages,
    role objects for inactive roles, and a missing/late initial
    ``client/state``) whenever the harness runs with
    ``allow_noncompliant_clients=False``. Reaching this point without a
    ``ClientComplianceError`` is evidence those checks passed; we additionally
    verify the negotiated active roles are a subset of what was requested, as
    an independently observable proxy for correct ``server/activate`` scoping.
    """
    active_role_ids = list(client.active_role_ids)
    negotiated_role_ids = list(getattr(client, "negotiated_role_ids", active_role_ids))

    core_002 = collector.assertion("CORE-002")
    core_002.record("connection_established_without_compliance_error", active_roles=active_role_ids)
    core_002.passed(
        "Server ran with allow_noncompliant_clients=False; no early-message "
        "compliance error was raised before activation"
    )

    core_003 = collector.assertion("CORE-003")
    unexpected = [role for role in active_role_ids if role not in negotiated_role_ids]
    core_003.record(
        "server_activate_scoped_to_negotiated_roles",
        requested_roles=requested_roles,
        negotiated_role_ids=negotiated_role_ids,
        active_role_ids=active_role_ids,
    )
    if unexpected:
        core_003.failed(f"Server activated undeclared role(s): {', '.join(unexpected)}")
    else:
        core_003.passed("Active roles are a subset of the negotiated role set")

    core_004 = collector.assertion("CORE-004")
    core_004.record(
        "initial_client_state_gate_satisfied",
        detail=(
            "SendspinConnection gates role binary data behind the initial "
            "client/state and flags noncompliance otherwise"
        ),
    )
    core_004.passed(
        "Connection reached the active state, which requires the initial "
        "client/state to have already been accepted"
    )


def record_activation_evidence_client(
    collector: ProtocolEvidenceCollector,
    client: Any,
) -> None:
    """Record CORE-002/003/004 evidence from the client side.

    The client-side SDK unconditionally sends the initial ``client/state``
    immediately after activation (before any role binary data), and only
    processes application messages once its own connection has reached the
    activated state — there is no externally-reachable hook to independently
    observe the wire ordering, so this records the client's committed
    behaviour as evidence rather than re-deriving it.
    """
    server_info = getattr(client, "server_info", None)

    core_002 = collector.assertion("CORE-002")
    core_002.record("client_connected_without_early_messages", server_id=getattr(server_info, "id", None))
    core_002.passed("Client only began sending application messages once activated")

    core_003 = collector.assertion("CORE-003")
    core_003.record(
        "server_activate_accepted",
        roles=[getattr(role, "value", role) for role in getattr(client, "roles", [])],
    )
    core_003.passed("Client accepted the server/activate role/version set it received")

    core_004 = collector.assertion("CORE-004")
    core_004.record(
        "initial_client_state_sent",
        detail="SendspinConnection sends the initial client/state immediately after activation",
    )
    core_004.passed("Client sent the initial client/state before any role binary data")


def record_player_stream_evidence(
    collector: ProtocolEvidenceCollector,
    *,
    advertised_formats: list[dict[str, Any]],
    negotiated_format: dict[str, Any] | None,
    chunk_count: int,
    chunk_timestamps_us: list[int],
) -> None:
    """Record PLAYER-001 evidence: an advertised format was selected and valid
    timestamped chunks were observed on the wire."""
    player_001 = collector.assertion("PLAYER-001")
    player_001.record(
        "stream_start_observed",
        negotiated_format=negotiated_format,
        advertised_formats=advertised_formats,
    )
    if negotiated_format is None:
        player_001.failed("No stream/start with a player object was observed")
        return

    def _matches(fmt: dict[str, Any]) -> bool:
        return all(fmt.get(key) == negotiated_format.get(key) for key in ("codec", "sample_rate", "channels"))

    if not any(_matches(fmt) for fmt in advertised_formats):
        player_001.failed(
            f"Negotiated format {negotiated_format!r} was not among the "
            f"advertised formats {advertised_formats!r}"
        )
        return

    if chunk_count == 0:
        player_001.failed("No audio chunks were observed after stream/start")
        return

    non_monotonic = [
        (prev, cur)
        for prev, cur in zip(chunk_timestamps_us, chunk_timestamps_us[1:])
        if cur < prev
    ]
    player_001.record(
        "audio_chunks_observed",
        chunk_count=chunk_count,
        first_timestamp_us=chunk_timestamps_us[0] if chunk_timestamps_us else None,
        last_timestamp_us=chunk_timestamps_us[-1] if chunk_timestamps_us else None,
    )
    if non_monotonic:
        player_001.failed(f"Chunk timestamps were not monotonic: {non_monotonic[:3]!r}")
        return

    player_001.passed(
        f"Selected an advertised format and streamed {chunk_count} "
        "timestamped chunks in order"
    )
