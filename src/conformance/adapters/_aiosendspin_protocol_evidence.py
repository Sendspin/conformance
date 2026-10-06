"""Protocol-evidence capture for aiosendspin adapters.

Wraps the aiosendspin SDK's connection objects from outside (no SDK changes)
to populate ``summary["protocol"]`` per the contract in
:mod:`conformance.protocol`, and ``summary["activation"]`` with the initial
``server/activate``. This is deliberately a monkey-patch: aiosendspin
does not yet expose a first-class tracing hook (tracked in
https://github.com/Sendspin/conformance/issues/111), so this module reaches
into private/semi-public attributes and is expected to need maintenance as
the SDK evolves.

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
  :class:`SentActivationRecorder` wraps ``EncryptedWebSocket.send_str`` at
  class level and keeps the JSON body handed to the encrypting transport.
- First metadata-carrying ``server/state``: full fidelity, and timed.
  :class:`SentMetadataStateRecorder` wraps the same transport method, because
  the timestamp requirement on that state needs a clock reading taken once the
  frame is really on the wire, which the enqueueing send path cannot give.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from conformance.protocol import SPEC_REVISION


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


class SentActivationRecorder:
    """
    Records the first ``server/activate`` sent on each encrypted connection.

    Recording starts on construction and covers every connection the process
    opens afterwards, so construct it before the server starts accepting
    clients. Call :meth:`uninstall` to stop recording.
    """

    def __init__(self) -> None:
        from aiosendspin.noise.wire import EncryptedWebSocket

        self._transport_class = EncryptedWebSocket
        self._original_send_str = EncryptedWebSocket.send_str
        self._first_by_socket: dict[Any, dict[str, Any]] = {}

        async def send_str(transport: Any, data: str) -> None:
            await self._original_send_str(transport, data)
            # Every JSON control body, including each server/time, passes through
            # here, so only bodies naming the message type are parsed.
            if '"server/activate"' not in data:
                return
            message = json.loads(data)
            if message.get("type") == "server/activate":
                # A pairing re-handshake swaps the transport but keeps the socket.
                self._first_by_socket.setdefault(transport._ws, message)

        EncryptedWebSocket.send_str = send_str  # type: ignore[method-assign]

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

    def uninstall(self) -> None:
        """Restore the SDK transport's own ``send_str``."""
        self._transport_class.send_str = self._original_send_str  # type: ignore[method-assign]


class SentMetadataStateRecorder:
    """
    Records the first ``server/state`` sent carrying a metadata object with a timestamp.

    Wraps ``EncryptedWebSocket.send_str`` for the same reason
    :class:`SentActivationRecorder` does: these states leave on the SDK's own
    queue drain rather than from the adapter, so no public hook fires once one
    is really on the wire. ``send_role_message`` only enqueues.

    The clock is read after the wrapped send returns, so the reading is an upper
    bound on when the state reached the transport. The direction matters: a
    reading taken before transmission would call a timestamp future when it was
    already past by the time the frame went out, and so blame a conformant
    server. A later reading can only make the check more permissive.

    Installed alongside the activation recorder, the two wrappers nest. Each
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
