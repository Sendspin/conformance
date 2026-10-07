"""Shared protocol-conformance evidence contract and assertions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .chunk_framing import HARNESS_GAP


SPEC_REVISION = "8c9577ea8719ad082d051ec13cc73ef15ed68948"

# The activities RC1 defines. An implementation's own extensions are not in it.
RC1_ACTIVITIES = frozenset({"pairing", "playback"})


@dataclass(frozen=True)
class ProtocolAssertion:
    """A normative requirement exercised by a protocol scenario."""

    id: str
    title: str
    specification_url: str


PROTOCOL_ASSERTIONS: dict[str, ProtocolAssertion] = {
    "CORE-001": ProtocolAssertion(
        id="CORE-001",
        title="Handshake messages use the required order and WebSocket frame types.",
        specification_url=(
            "https://github.com/Sendspin/spec/blob/"
            f"{SPEC_REVISION}/messaging.md#communication"
        ),
    ),
    "CORE-002": ProtocolAssertion(
        id="CORE-002",
        title="No application message is exchanged before the first server/activate.",
        specification_url=(
            "https://github.com/Sendspin/spec/blob/"
            f"{SPEC_REVISION}/messaging.md#communication"
        ),
    ),
    "CORE-003": ProtocolAssertion(
        id="CORE-003",
        title="The server activates only advertised role versions with required support objects.",
        specification_url=(
            "https://github.com/Sendspin/spec/blob/"
            f"{SPEC_REVISION}/messaging.md#client--server-clienthello"
        ),
    ),
    "CORE-004": ProtocolAssertion(
        id="CORE-004",
        title="Each activated client sends an initial client/state before role binary data.",
        specification_url=(
            "https://github.com/Sendspin/spec/blob/"
            f"{SPEC_REVISION}/messaging.md#client--server-clientstate"
        ),
    ),
    "PLAYER-001": ProtocolAssertion(
        id="PLAYER-001",
        title="A player stream selects an advertised format and sends valid timestamped chunks.",
        specification_url=(
            "https://github.com/Sendspin/spec/blob/"
            f"{SPEC_REVISION}/roles/player/v1.md#server--client-streamstart-player-object"
        ),
    ),
}


def protocol_evidence_failure(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
    *,
    assertion_ids: tuple[str, ...],
) -> str | None:
    """Return a precise missing or failed protocol-evidence reason."""
    unknown = [assertion_id for assertion_id in assertion_ids if assertion_id not in PROTOCOL_ASSERTIONS]
    if unknown:
        return f"Scenario references unknown protocol assertion(s): {', '.join(unknown)}"

    for role, summary in (("server", server_summary), ("client", client_summary)):
        protocol = summary.get("protocol")
        if not isinstance(protocol, dict):
            return f"{role.capitalize()} summary is missing required protocol evidence"
        if protocol.get("spec_revision") != SPEC_REVISION:
            return (
                f"{role.capitalize()} protocol evidence targets spec revision "
                f"{protocol.get('spec_revision')!r}, expected {SPEC_REVISION!r}"
            )
        assertions = protocol.get("assertions")
        if not isinstance(assertions, dict):
            return f"{role.capitalize()} protocol evidence is missing its assertions map"
        for assertion_id in assertion_ids:
            result = assertions.get(assertion_id)
            if not isinstance(result, dict):
                return f"{role.capitalize()} protocol evidence is missing {assertion_id}"
            if result.get("status") != "passed":
                detail = result.get("detail")
                suffix = f": {detail}" if isinstance(detail, str) and detail else ""
                return f"{role.capitalize()} did not pass {assertion_id}{suffix}"
            events = result.get("events")
            if not isinstance(events, list) or not events:
                return f"{role.capitalize()} {assertion_id} has no trace events"
    return None


def activation_violation(server_summary: dict[str, Any]) -> str | None:
    """
    Return how the server's initial `server/activate` breaks RC1, or None.

    RC1 ends the mandatory initial sequence of every connection with a
    `server/activate`, so this is judged on every case. Three things are
    asserted against the message as the server's adapter recorded it: that one
    was sent, that its `activities` is a list of unique values RC1 defines, and
    that it carries `active_roles`. Every reason names the server as the party
    at fault.

    Which activity sets and roles are allowed depends on which PSK matched
    during the handshake. No summary records that, so neither is judged.

    None covers both a conformant activation and a summary no activation can be
    read from: no `activation` field, or a value that is not a
    `server/activate` message. An unreadable recording is not evidence against
    an implementation. A recorded `null` is evidence: the adapter contract
    reserves it for a server that sent no activation at all.
    """
    if "activation" not in server_summary:
        return None
    activation = server_summary["activation"]
    if activation is None:
        return (
            "Server sent no initial server/activate; RC1 requires the server to send one "
            "on every connection, as the last step of the initial sequence"
        )
    if not isinstance(activation, dict) or activation.get("type") != "server/activate":
        return None
    payload = activation.get("payload")
    if not isinstance(payload, dict):
        return None

    defects: list[str] = []
    if "activities" not in payload:
        defects.append("omitted activities, which RC1 requires")
    elif not _is_rc1_activity_set(payload["activities"]):
        defects.append(
            f"declared activities {json.dumps(payload['activities'])}, where RC1 requires "
            "a list of unique values drawn from 'pairing' and 'playback'"
        )
    if "active_roles" not in payload:
        defects.append("omitted active_roles, which RC1 requires on the first activation")
    if not defects:
        return None
    return f"Server's initial server/activate {'; '.join(defects)}"


_GATE_RULE = (
    "RC1 forbids stream/start unless the latest client/state the server received "
    "reports available: true"
)
_NO_CLIENT_STATE = object()


def stream_start_gate_violation(server_summary: dict[str, Any]) -> str | None:
    """
    Return how a `stream/start` the server sent breaks RC1's available gate, or None.

    Judged from `availability_trace`, the server adapter's ordered record of the
    `client/state` messages it received and the `stream/start` messages it sent.
    Each `stream/start` is bracketed by a `sending` entry and a `sent` entry, and
    passes when the latest `client/state` reported `available: true` at any point
    between the two. A state that lands while the frame is being written cannot
    be placed on either side of it, so the bracket gives the server the benefit
    of both orders. A pass therefore means no violation was witnessed.

    A failing `stream/start` is described by the latest `client/state` received
    before its `sending` entry, the one the server certainly held when it let
    the frame go. None at all, or one that reported `available: false`, names
    the server. One that carried no boolean `available` names the client first,
    because the client broke its own requirement before the server could meet
    this one.

    A trace that is absent, null or unreadable is a harness gap, and so is one
    holding no `stream/start` that reached the wire: this is only asked of a
    case that opened a stream, so the gate was not witnessed, which is neither
    a violation nor compliance. A `stream/start` whose `sending` entry has no
    `sent` entry never reached the wire and is not judged.
    """
    unrecorded = (
        f"{HARNESS_GAP}the server adapter did not record the client/state messages it "
        "received and the stream/start messages it sent, so the available gate on "
        "stream/start cannot be judged"
    )
    entries = _gate_entries(server_summary.get("availability_trace"))
    if entries is None:
        return unrecorded

    latest: Any = _NO_CLIENT_STATE
    # Set while a stream/start is in flight: the latest state when it was handed
    # over, and whether available: true has been the latest state since.
    held_at_sending: Any = None
    available_in_flight: bool | None = None
    stream_starts = 0
    for kind, value in entries:
        if kind == "client/state":
            latest = value
            if available_in_flight is not None:
                available_in_flight = available_in_flight or value is True
        elif kind == "sending":
            held_at_sending = latest
            available_in_flight = latest is True
        elif available_in_flight is None:
            # A `sent` that no `sending` opened brackets nothing.
            return unrecorded
        else:
            stream_starts += 1
            if not available_in_flight:
                return _gate_violation_reason(held_at_sending, roles=value)
            available_in_flight = None

    if not stream_starts:
        return (
            f"{HARNESS_GAP}the server adapter recorded no stream/start reaching the wire "
            "in a case that opened a stream, so the available gate on stream/start "
            "cannot be judged"
        )
    return None


def _gate_entries(trace: Any) -> list[tuple[str, Any]] | None:
    """
    Return a trace as (kind, value) pairs, or None when it cannot be read.

    `kind` is "client/state" with the `available` it carried, or the `phase` of
    a `stream/start` with the roles it named.
    """
    if not isinstance(trace, list):
        return None
    entries: list[tuple[str, Any]] = []
    for entry in trace:
        if not isinstance(entry, dict):
            return None
        if entry.get("type") == "client/state":
            entries.append(("client/state", entry.get("available")))
        elif entry.get("type") == "stream/start" and entry.get("phase") in ("sending", "sent"):
            entries.append((entry["phase"], entry.get("roles")))
        else:
            return None
    return entries


def _gate_violation_reason(latest: Any, *, roles: Any) -> str:
    named = f" ({', '.join(map(str, roles))})" if isinstance(roles, list) and roles else ""
    if latest is _NO_CLIENT_STATE:
        return (
            f"Server sent stream/start{named} before it had received any client/state; "
            f"{_GATE_RULE}"
        )
    if latest is False:
        return (
            f"Server sent stream/start{named} while the latest client/state it had "
            f"received reported available: false; {_GATE_RULE}"
        )
    return (
        "Client sent client/state without a boolean `available`, which RC1 requires on "
        f"every client/state; the server then sent stream/start{named}, and {_GATE_RULE}"
    )


def _is_rc1_activity_set(activities: Any) -> bool:
    if not isinstance(activities, list):
        return False
    if not all(isinstance(activity, str) and activity in RC1_ACTIVITIES for activity in activities):
        return False
    return len(set(activities)) == len(activities)
