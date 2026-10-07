"""Shared protocol-conformance evidence contract and assertions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .chunk_framing import HARNESS_GAP


# The revision the protocol-baseline assertions below cite, and the one an
# adapter's protocol evidence must name. It is not the revision the matrix is
# audited against: that is the spec checkout each run records in
# `repositories.json`, and the verdicts applied to every case, which say RC1,
# are judged against that.
SPEC_REVISION = "8c9577ea8719ad082d051ec13cc73ef15ed68948"

# The activities RC1 defines. An implementation's own extensions are not in it.
RC1_ACTIVITIES = frozenset({"pairing", "playback"})

# The group playback states RC1 defines.
RC1_PLAYBACK_STATES = ("playing", "stopped")


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


def group_update_violation(server_summary: dict[str, Any]) -> str | None:
    """
    Return how the server's `group/update` messages break RC1, or None.

    RC1 has the server send a `group/update` after the first `server/activate`
    on every connection, and has every `group/update` carry all three of its
    fields, so this is judged on every case. It is asserted against the
    messages as the server's adapter recorded them: that at least one followed
    the first `server/activate`, and that in each one `playback_state` is
    'playing' or 'stopped' and `group_id` and `group_name` are strings.

    RC1 says the first follows "promptly" and gives no bound, so how long the
    server took is not judged: one sent at any point in the case satisfies it.
    Whether a change to one of the fields produced a further `group/update` is
    not judged either.

    None means every recorded `group/update` conforms, with one exception: a
    server recorded as sending no `server/activate` gave the requirement
    nothing to follow, and `activation_violation` already reports it.

    A recorded empty list is a server defect: the adapter contract reserves it
    for a server that sent none. So is a `group/update` with no payload
    object. A summary with no `group_updates` list, or a list holding some
    other message, is reported as a harness gap, naming the adapter rather
    than the implementation.
    """
    if "activation" in server_summary and server_summary["activation"] is None:
        return None
    group_updates = server_summary.get("group_updates")
    if not isinstance(group_updates, list):
        return (
            f"{HARNESS_GAP}the server adapter does not record the group/update messages "
            "it sent after its first server/activate"
        )
    if not group_updates:
        return (
            "Server sent no group/update after its first server/activate; RC1 requires "
            "the server to send one on every connection"
        )
    for index, group_update in enumerate(group_updates, start=1):
        if not isinstance(group_update, dict) or group_update.get("type") != "group/update":
            return (
                f"{HARNESS_GAP}the server adapter recorded something other than a "
                f"group/update among those sent: {json.dumps(group_update)}"
            )
        defects = _group_update_defects(group_update.get("payload"))
        if defects:
            return (
                f"Server's group/update {index} of {len(group_updates)} after "
                f"server/activate {'; '.join(defects)}"
            )
    return None


def _group_update_defects(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["carried no payload object, so none of the fields RC1 requires"]
    defects: list[str] = []
    if "playback_state" not in payload:
        defects.append("omitted playback_state, which RC1 requires")
    elif payload["playback_state"] not in RC1_PLAYBACK_STATES:
        defects.append(
            f"declared playback_state {json.dumps(payload['playback_state'])}, where RC1 "
            "requires 'playing' or 'stopped'"
        )
    for name in ("group_id", "group_name"):
        if name not in payload:
            defects.append(f"omitted {name}, which RC1 requires")
        elif not isinstance(payload[name], str):
            defects.append(
                f"declared {name} {json.dumps(payload[name])}, where RC1 requires a string"
            )
    return defects


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


_TIME_FIELDS = ("client_transmitted", "server_received", "server_transmitted")


def time_exchange_violation(server_summary: dict[str, Any]) -> str | None:
    """
    Return how the `client/time` to `server/time` exchange breaks RC1, or None.

    Judged from `time_exchange`, the server adapter's ordered record of each
    `client/time` it received and each `server/time` it sent. Clock sync is
    core messaging on every connection, so this is judged on every case. Four
    things are asserted of the server: that each `server/time` carries
    `client_transmitted`, `server_received` and `server_transmitted` as
    integers, that its `client_transmitted` is the value of a `client/time`
    the server had received and not yet answered, that `server_received` is
    not later than `server_transmitted`, and that no `client/time` went
    unanswered.

    RC1 does not state the third as a rule. It follows from both timestamps
    being readings of the server's one monotonic clock, taken at two events
    that happen in that order.

    RC1 gives no bound on how soon the response follows, and a connection can
    close with a `client/time` still unread. So one left unanswered is a
    violation only when the server answered a `client/time` it received later,
    or answered none at all. Both halves are inferences. The first also names
    a server that answers out of order and was cut off between two replies,
    and the second a server that received its only `client/time` as the
    connection closed. A server that stops answering partway through is not
    caught: every `client/time` it ignored trails its last `server/time`.

    The values themselves are not judged, because RC1 says the timestamps are
    not necessarily epoch-based. Nor is whether `server_transmitted` was
    stamped as late as RC1 requires, which needs a transmit instant no summary
    carries, nor whether the client's time filter converged, which no message
    reports.

    A `client/time` whose `client_transmitted` is not an integer names the
    client, which broke its own requirement before the server could echo it.

    None covers a conformant exchange and a case in which the client sent no
    `client/time`. A summary with no `time_exchange` list, or a list holding
    anything else, is reported as a harness gap, naming the adapter rather
    than the implementation.
    """
    exchange = server_summary.get("time_exchange")
    if not isinstance(exchange, list):
        return (
            f"{HARNESS_GAP}the server adapter did not record the client/time messages it "
            "received and the server/time messages it sent, so the exchange cannot be judged"
        )

    # Position and client_transmitted of each client/time not yet answered.
    unanswered: list[tuple[int, int]] = []
    received = 0
    answered = 0
    latest_answered = 0
    for entry in exchange:
        kind = entry.get("type") if isinstance(entry, dict) else None
        if kind not in ("client/time", "server/time"):
            return (
                f"{HARNESS_GAP}the server adapter recorded something other than a "
                f"client/time or server/time in the exchange: {json.dumps(entry)}"
            )
        payload = entry.get("payload")
        if kind == "client/time":
            received += 1
            sent = payload.get("client_transmitted") if isinstance(payload, dict) else None
            if not _is_integer(sent):
                return (
                    f"Client's client/time {received} carried client_transmitted "
                    f"{json.dumps(sent)}, where RC1 requires an integer"
                )
            unanswered.append((received, sent))
            continue

        answered += 1
        defects = _server_time_defects(payload)
        if defects:
            return f"Server's server/time {answered} {'; '.join(defects)}"
        echoed = payload["client_transmitted"]
        match = next((item for item in unanswered if item[1] == echoed), None)
        if match is None:
            return (
                f"Server's server/time {answered} carried client_transmitted {echoed}, "
                "which is not the value of any client/time it had received and not yet "
                "answered; RC1 defines the field as the timestamp received in the client/time"
            )
        unanswered.remove(match)
        latest_answered = max(latest_answered, match[0])
        if payload["server_received"] > payload["server_transmitted"]:
            return (
                f"Server's server/time {answered} carried server_received "
                f"{payload['server_received']} later than its server_transmitted "
                f"{payload['server_transmitted']}; both are readings of the server's "
                "monotonic clock, and it receives the client/time before it responds"
            )

    if received and not answered:
        return (
            f"Server received {received} client/time and sent no server/time; RC1 has the "
            "server respond to each with a server/time"
        )
    skipped = [position for position, _ in unanswered if position < latest_answered]
    if skipped:
        return (
            f"Server sent no server/time for client/time {skipped[0]} of {received}, yet "
            "answered one it received later; RC1 has the server respond to each with a "
            "server/time"
        )
    return None


def _server_time_defects(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["carried no payload object, so none of the fields RC1 requires"]
    defects: list[str] = []
    for name in _TIME_FIELDS:
        if name not in payload:
            defects.append(f"omitted {name}, which RC1 requires")
        elif not _is_integer(payload[name]):
            defects.append(
                f"declared {name} {json.dumps(payload[name])}, where RC1 requires an integer"
            )
    return defects


def _is_integer(value: Any) -> bool:
    # bool is an int subclass, and JSON true is not a timestamp.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_rc1_activity_set(activities: Any) -> bool:
    if not isinstance(activities, list):
        return False
    if not all(isinstance(activity, str) and activity in RC1_ACTIVITIES for activity in activities):
        return False
    return len(set(activities)) == len(activities)
