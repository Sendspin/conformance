"""
Machine checks for the unpaired, pairing and post-pairing suites.

Each check is a pure function of the `ConnectionRecord` of the connection its step
ran on, judged once the step is over. References are to the Sendspin spec at tag
`1.0.0-rc1`. Which PSK keyed a handshake is not in any frame, since the Noise
payload naming it is encrypted, so those checks read the KEYED and
CREDENTIAL_MISMATCH events the session recorded from aiosendspin.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from conformance._vendor.aiosendspin.noise import decode_psk_token

from .checks import ConnectionRecord
from .findings import Finding, Verdict
from .recorder import EventKind, RecordedFrame

SPEC_REFS = {
    "UNPAIRED-001": "connection.md:105, connection.md:119",
    "UNPAIRED-002": "messaging.md:251-253, messaging.md:257",
    "UNPAIRED-003": "pairing.md:47, messaging.md:297-299",
    "UNPAIRED-004": "messaging.md:251-255",
    "PAIR-PSK-001": "pairing.md:246-268",
    "PAIR-PSK-002": "pairing.md:86-95",
    "PAIR-PSK-003": "pairing.md:79, pairing.md:83, connection.md:147-151",
    "PAIR-PSK-004": "pairing.md:59, pairing.md:84, pairing.md:327, pairing.md:338-341",
    "PAIR-PSK-005": "pairing.md:59, pairing.md:388-390",
    "PAIR-PSK-006": "pairing.md:3, pairing.md:23, connection.md:147-151",
    "PAIR-PSK-007": "messaging.md:239-241, connection.md:151",
    "POSTPAIR-001": "connection.md:119-121, pairing.md:23",
    "POSTPAIR-002": "connection.md:119, connection.md:123-129",
    "POSTPAIR-003": "messaging.md:450-456, messaging.md:473",
    "POSTPAIR-004": "messaging.md:456, connection.md:125",
    "POSTPAIR-005": "pairing.md:23",
    "POSTPAIR-006": "roles/player/v1.md:59",
}
# aiosendspin's PskCategory values, as KEYED events record them.
LONG_TERM, PAIRING, SENTINEL = "long_term", "pairing", "sentinel"
# The client answered under the Sentinel after a long-term PSK lookup miss.
FALLBACK = "sentinel_fallback"
_KEYING_NAMES = {
    LONG_TERM: "the long-term PSK",
    PAIRING: "the pairing PSK",
    SENTINEL: "the Sentinel PSK",
    FALLBACK: "a Sentinel fallback from the long-term PSK",
}
# The PSK the validator's Noise message 1 referenced for each outcome: only the
# client's answer to that reference is its own doing.
_REFERENCED = {
    LONG_TERM: "the long-term PSK",
    PAIRING: "the pairing PSK",
    SENTINEL: "the Sentinel PSK",
    FALLBACK: "the long-term PSK",
}
# Client messages that end a pairing attempt or the connection.
_REFUSALS = frozenset({"pair/abort", "client/goodbye"})
# Client pairing messages that have no place in the Pairing PSK flow after pair-init.
_PAIRING_MESSAGES = frozenset(
    {
        "client/pair-init",
        "client/pair-pending",
        "client/pair-auth",
        "client/pair-confirm",
        "client/pair-retry",
    }
)


def check_pairing_token(token: str, client_id: str | None) -> list[Finding]:
    """
    PAIR-PSK-001 and PAIR-PSK-002: the token decodes, and names the connected client.

    `token` is the text the user pasted, decoded leniently as the spec requires.
    """
    try:
        decoded = decode_psk_token(token)
    except ValueError as err:
        return [
            _finding("PAIR-PSK-001", Verdict.FAIL, f"the pairing token does not decode: {err}"),
            _finding("PAIR-PSK-002", Verdict.NOT_APPLICABLE, "no decoded token to compare"),
        ]
    decodes = _finding("PAIR-PSK-001", Verdict.PASS, "the pairing token decodes as version 0")
    if client_id is None:
        return [
            decodes,
            _finding("PAIR-PSK-002", Verdict.NOT_APPLICABLE, "the client sent no client_id"),
        ]
    if decoded.client_id != client_id:
        return [
            decodes,
            _finding(
                "PAIR-PSK-002",
                Verdict.FAIL,
                f"the token's client_key is {decoded.client_id}, not the connected client_id "
                f"{client_id}",
            ),
        ]
    return [
        decodes,
        _finding("PAIR-PSK-002", Verdict.PASS, "the token's client_key is the connected client_id"),
    ]


def check_sentinel_handshake(record: ConnectionRecord) -> list[Finding]:
    """UNPAIRED-001: with no pairing record, the client completes a Sentinel-keyed handshake."""
    return _judge_keying(
        record,
        "UNPAIRED-001",
        SENTINEL,
        "the client completed the handshake on the Sentinel PSK",
    )


def check_pairing_required(record: ConnectionRecord) -> list[Finding]:
    """
    UNPAIRED-002: with unpaired access off, the client refuses playback with pairing_required.

    The validator declared `['playback']` with roles on a Sentinel session whose
    client/hello reported unpaired access disabled. Enabling it would make that
    activation admissible, so the first rule of messaging.md:251 applies.
    """
    if unpaired_access(record) is not False:
        return [_n_a("UNPAIRED-002", "the client/hello did not report unpaired access disabled")]
    activation = _first_outbound_activation(record, lambda p: "playback" in activities(p))
    if activation is None:
        return [_n_a("UNPAIRED-002", "the validator did not declare playback")]
    return _judge_refusal(record, "UNPAIRED-002", activation, "pairing_required")


def check_unpaired_playback(record: ConnectionRecord) -> list[Finding]:
    """UNPAIRED-003: with unpaired access on, the client accepts roles and reports client/state."""
    if unpaired_access(record) is not True:
        return [_n_a("UNPAIRED-003", "the client/hello did not report unpaired access enabled")]
    activation = _first_outbound_activation(record, lambda p: bool(p.get("active_roles")))
    if activation is None:
        return [
            _finding(
                "UNPAIRED-003",
                Verdict.TOOL_ERROR,
                "the validator activated no roles, though unpaired access is enabled",
            )
        ]
    response = first_inbound(record, activation, {"client/state", *_REFUSALS})
    if response is not None and response.type == "client/state":
        return [
            _finding(
                "UNPAIRED-003",
                Verdict.PASS,
                "the client accepted the unpaired activation and sent client/state",
                activation,
                response,
            )
        ]
    if response is not None:
        return [
            _finding(
                "UNPAIRED-003",
                Verdict.FAIL,
                f"the client refused an admissible unpaired activation with {describe(response)}",
                activation,
                response,
            )
        ]
    if record.rejected:
        return [_not_reached("UNPAIRED-003")]
    return [
        _finding(
            "UNPAIRED-003",
            Verdict.FAIL,
            "no client/state followed the unpaired activation of "
            + ", ".join(map(str, (activation.payload or {}).get("active_roles") or [])),
            activation,
        )
    ]


def check_inadmissible_activation(record: ConnectionRecord) -> list[Finding]:
    """
    UNPAIRED-004: the client rejects an inadmissible activation by the rule order.

    The validator declared `['playback', 'pairing']` with method `pairing_psk` on a
    Sentinel session, which no unpaired access makes admissible. With unpaired access
    off the activity set is not allowed, so the reason is `unauthorized`; with it on,
    only the method is wrong, so the client aborts with `method_not_supported` and
    keeps the connection.
    """
    activation = _first_outbound_activation(record, lambda p: "pairing" in activities(p))
    if activation is None:
        return [_n_a("UNPAIRED-004", "the validator sent no inadmissible activation")]
    match unpaired_access(record):
        case False:
            return _judge_refusal(record, "UNPAIRED-004", activation, "unauthorized")
        case True:
            return _judge_abort(record, activation)
        case _:
            return [_n_a("UNPAIRED-004", "the client/hello did not report unpaired access")]


def check_pairing_rehandshake(record: ConnectionRecord, since: int) -> list[Finding]:
    """PAIR-PSK-003: the client completes the re-handshake onto its pairing PSK."""
    message_1 = first_after(record, since, lambda f: is_out(f, "noise/handshake"))
    if message_1 is None:
        return [_n_a("PAIR-PSK-003", "the validator did not re-handshake to the pairing PSK")]
    message_2 = first_after(record, message_1.id, lambda f: is_in(f, "noise/handshake"))
    if message_2 is None:
        if record.rejected:
            return [_not_reached("PAIR-PSK-003")]
        return [
            _finding(
                "PAIR-PSK-003",
                Verdict.FAIL,
                "the client did not answer the re-handshake onto its pairing PSK, which it "
                "must keep among its handshake candidates",
                message_1,
            )
        ]
    activation = _pairing_activation(record, message_2.id)
    if activation is None:
        if record.rejected:
            return [_not_reached("PAIR-PSK-003")]
        return [
            _finding(
                "PAIR-PSK-003",
                Verdict.FAIL,
                "the client's Noise message 2 did not verify under its pairing PSK",
                message_1,
                message_2,
            )
        ]
    return [
        _finding(
            "PAIR-PSK-003",
            Verdict.PASS,
            "the client completed the re-handshake onto its pairing PSK",
            message_1,
            message_2,
        )
    ]


def check_pair_init(record: ConnectionRecord, since: int) -> list[Finding]:
    """PAIR-PSK-004: the pairing activation is answered with a matching client/pair-init."""
    activation = _pairing_activation(record, since)
    if activation is None:
        return [_n_a("PAIR-PSK-004", "the validator sent no pairing_psk activation")]
    response = first_inbound(
        record, activation, {"client/pair-init", "client/pair-finalize", *_REFUSALS}
    )
    if response is None:
        if record.rejected:
            return [_not_reached("PAIR-PSK-004")]
        return [
            _finding(
                "PAIR-PSK-004",
                Verdict.FAIL,
                "the client did not answer the pairing activation with client/pair-init",
                activation,
            )
        ]
    if response.type != "client/pair-init":
        return [
            _finding(
                "PAIR-PSK-004",
                Verdict.FAIL,
                f"the client answered the pairing activation with {describe(response)}, "
                "not client/pair-init",
                activation,
                response,
            )
        ]
    payload = response.payload or {}
    expected_index = pairing_activations_since_handshake(record, activation)
    problems = []
    if payload.get("pairing_index") != expected_index or isinstance(
        payload.get("pairing_index"), bool
    ):
        problems.append(f"pairing_index is {payload.get('pairing_index')!r}, not {expected_index}")
    if "commit_B" in payload:
        problems.append("it carries commit_B, which only the dynamic pairing code flow sends")
    if problems:
        return [_finding("PAIR-PSK-004", Verdict.FAIL, "; ".join(problems), activation, response)]
    return [
        _finding(
            "PAIR-PSK-004",
            Verdict.PASS,
            f"the client started the attempt with client/pair-init, pairing_index {expected_index}",
            activation,
            response,
        )
    ]


def check_pair_finalize(record: ConnectionRecord, since: int) -> list[Finding]:
    """
    PAIR-PSK-005: client/pair-init is followed straight away by client/pair-finalize.

    The finalize must carry `long_term_psk` and no `wrapped_psk`. The recorder redacts
    the key, so its 32-byte, 43-character encoding is judged by aiosendspin, which
    acknowledges it with server/pair-finalize only once the key decodes.
    """
    pair_init = first_after(record, since, lambda f: is_in(f, "client/pair-init"))
    if pair_init is None:
        return [_n_a("PAIR-PSK-005", "the client sent no client/pair-init")]
    finalize = first_inbound(
        record, pair_init, {"client/pair-finalize", *_PAIRING_MESSAGES, *_REFUSALS}
    )
    if finalize is None or finalize.type != "client/pair-finalize":
        if finalize is None and record.rejected:
            return [_not_reached("PAIR-PSK-005")]
        after = f", but {describe(finalize)}" if finalize is not None else ""
        return [
            _finding(
                "PAIR-PSK-005",
                Verdict.FAIL,
                f"the client did not follow client/pair-init with client/pair-finalize{after}",
                pair_init,
                *(finalize,) if finalize is not None else (),
            )
        ]
    payload = finalize.payload or {}
    problems = []
    if not isinstance(payload.get("long_term_psk"), str):
        problems.append("client/pair-finalize carries no long_term_psk string")
    if "wrapped_psk" in payload:
        problems.append("client/pair-finalize carries wrapped_psk, which only code flows send")
    if problems:
        return [_finding("PAIR-PSK-005", Verdict.FAIL, "; ".join(problems), pair_init, finalize)]
    acknowledged = first_after(record, finalize.id, lambda f: is_out(f, "server/pair-finalize"))
    if acknowledged is None:
        return [
            _finding(
                "PAIR-PSK-005",
                Verdict.FAIL,
                "aiosendspin did not accept the long_term_psk: it is not a 32-byte key in "
                "43 characters of base64url, or it reuses the pairing or Sentinel PSK",
                finalize,
            )
        ]
    return [
        _finding(
            "PAIR-PSK-005",
            Verdict.PASS,
            "the client delivered a valid long_term_psk right after client/pair-init",
            pair_init,
            finalize,
            acknowledged,
        )
    ]


def check_promotion(record: ConnectionRecord, since: int) -> list[Finding]:
    """PAIR-PSK-006: after server/pair-finalize, the client re-handshakes to the long-term PSK."""
    acknowledged = first_after(record, since, lambda f: is_out(f, "server/pair-finalize"))
    if acknowledged is None:
        return [_n_a("PAIR-PSK-006", "the pairing did not reach server/pair-finalize")]
    message_1 = first_after(record, acknowledged.id, lambda f: is_out(f, "noise/handshake"))
    message_2 = message_1 and first_after(
        record, message_1.id, lambda f: is_in(f, "noise/handshake")
    )
    keyed = next(
        (
            e
            for e in record.events
            if e.kind is EventKind.KEYED and (e.after_frame or 0) > acknowledged.id
        ),
        None,
    )
    if message_1 is None:
        return [_n_a("PAIR-PSK-006", "the validator did not re-handshake after the finalize")]
    if message_2 is None:
        if record.rejected:
            return [_not_reached("PAIR-PSK-006")]
        return [
            _finding(
                "PAIR-PSK-006",
                Verdict.FAIL,
                "the client did not answer the re-handshake to the long-term PSK it delivered",
                acknowledged,
                message_1,
            )
        ]
    if keyed is None or keyed.psk_category != LONG_TERM:
        return [
            _finding(
                "PAIR-PSK-006",
                Verdict.FAIL,
                "the re-handshake after server/pair-finalize did not key the session with the "
                "long-term PSK",
                message_1,
                message_2,
            )
        ]
    return [
        _finding(
            "PAIR-PSK-006",
            Verdict.PASS,
            "the client re-handshook to the long-term PSK it delivered",
            message_1,
            message_2,
        )
    ]


def check_paired_activation(record: ConnectionRecord, since: int) -> list[Finding]:
    """PAIR-PSK-007: the client accepts the activation that follows the long-term re-handshake."""
    acknowledged = first_after(record, since, lambda f: is_out(f, "server/pair-finalize"))
    message_2 = acknowledged and first_after(
        record, acknowledged.id, lambda f: is_in(f, "noise/handshake")
    )
    activation = message_2 and first_after(
        record, message_2.id, lambda f: is_out(f, "server/activate")
    )
    if activation is None:
        return [_n_a("PAIR-PSK-007", "no activation followed a long-term re-handshake")]
    refusal = first_inbound(record, activation, _REFUSALS)
    if refusal is not None:
        return [
            _finding(
                "PAIR-PSK-007",
                Verdict.FAIL,
                f"the client refused the paired activation with {describe(refusal)}",
                activation,
                refusal,
            )
        ]
    if record.closed:
        return [
            _finding(
                "PAIR-PSK-007",
                Verdict.FAIL,
                "the connection ended after the paired activation",
                activation,
            )
        ]
    return [
        _finding(
            "PAIR-PSK-007",
            Verdict.PASS,
            "the client accepted the activation on the paired session",
            activation,
        )
    ]


def check_long_term_reconnect(record: ConnectionRecord) -> list[Finding]:
    """
    POSTPAIR-001: reconnecting while paired matches the long-term PSK.

    This also covers R2: the client accepts the record only after checking its stored
    server_id against server/init (connection.md:121).
    """
    return _judge_keying(
        record,
        "POSTPAIR-001",
        LONG_TERM,
        "the client matched the long-term PSK from pairing, bound to this server_id",
    )


def check_forgotten_reconnect(record: ConnectionRecord) -> list[Finding]:
    """
    POSTPAIR-002: the client accepts the Sentinel once the validator forgets the pairing.

    The validator discarded its record but kept its identity, so it references the
    Sentinel PSK while the client still holds a long-term record for this server_id.
    The validator must activate no playback or roles.
    """
    declared = _first_outbound_activation(
        record, lambda p: "playback" in activities(p) or bool(p.get("active_roles"))
    )
    if declared is not None:
        return [
            _finding(
                "POSTPAIR-002",
                Verdict.TOOL_ERROR,
                "the validator activated playback or roles on a forgotten client",
                declared,
            )
        ]
    return _judge_keying(
        record,
        "POSTPAIR-002",
        SENTINEL,
        "the client accepted the Sentinel handshake from a server that forgot it",
    )


def check_unpair_goodbye(record: ConnectionRecord) -> list[Finding]:
    """POSTPAIR-003: on a paired session, server/unpair is answered with goodbye 'unpaired'."""
    unpair = next(iter(record.outbound("server/unpair")), None)
    if unpair is None:
        return [_n_a("POSTPAIR-003", "the validator sent no server/unpair")]
    keyed = [
        e for e in record.events if e.kind is EventKind.KEYED and (e.after_frame or 0) < unpair.id
    ]
    if not keyed or keyed[-1].psk_category != LONG_TERM:
        return [_n_a("POSTPAIR-003", "the session was not keyed by the long-term PSK")]
    goodbye = first_inbound(record, unpair, {"client/goodbye"})
    if goodbye is None:
        return [
            _finding(
                "POSTPAIR-003",
                Verdict.FAIL,
                "the client did not answer server/unpair with client/goodbye 'unpaired'",
                unpair,
            )
        ]
    reason = (goodbye.payload or {}).get("reason")
    if reason != "unpaired":
        return [
            _finding(
                "POSTPAIR-003",
                Verdict.FAIL,
                f"the client answered server/unpair with goodbye {reason!r}, not 'unpaired'",
                unpair,
                goodbye,
            )
        ]
    return [
        _finding(
            "POSTPAIR-003",
            Verdict.PASS,
            "the client answered server/unpair with client/goodbye 'unpaired'",
            unpair,
            goodbye,
        )
    ]


def check_record_dropped(record: ConnectionRecord) -> list[Finding]:
    """
    POSTPAIR-004: after server/unpair the client no longer holds the pairing record.

    The validator references the record it unpaired, so a client that dropped it
    misses the lookup and falls back to the Sentinel.
    """
    return _judge_keying(
        record,
        "POSTPAIR-004",
        FALLBACK,
        "the client no longer holds the unpaired record and fell back to the Sentinel",
        kept="the client still holds the pairing record it was told to drop by server/unpair",
    )


def check_restart_keeps_pairing(record: ConnectionRecord) -> list[Finding]:
    """POSTPAIR-005: after a full restart the client still matches the long-term PSK."""
    return _judge_keying(
        record,
        "POSTPAIR-005",
        LONG_TERM,
        "the client kept its pairing record across the restart",
    )


def check_restart_keeps_output_delay(
    before: ConnectionRecord, after: ConnectionRecord, commanded: int | None
) -> list[Finding]:
    """
    POSTPAIR-006: the output delay the validator set before a restart is reported after it.

    `commanded` is the delay the client reported back on `before` after the validator
    set it, or None when it could not be set.
    """
    if commanded is None:
        return [
            _n_a(
                "POSTPAIR-006",
                "the validator could not set an output delay: the player does not offer "
                "set_output_delay, or did not report the delay it was set to",
            )
        ]
    echo = next(f for f in before.inbound("client/state") if _output_delay(f) == commanded)
    state = next((f for f in after.inbound("client/state") if _output_delay(f) is not None), None)
    if state is None:
        return [_n_a("POSTPAIR-006", "the client reported no player output_delay_ms after")]
    delay = _output_delay(state)
    if delay != commanded:
        return [
            _finding(
                "POSTPAIR-006",
                Verdict.FAIL,
                f"output_delay_ms was set to {commanded} before the restart and is {delay} after",
                echo,
                state,
            )
        ]
    return [
        _finding(
            "POSTPAIR-006",
            Verdict.PASS,
            f"output_delay_ms stayed {delay} across the restart",
            echo,
            state,
        )
    ]


def unpaired_access(record: ConnectionRecord) -> bool | None:
    """Return `unpaired_access.enabled` from the connection's client/hello, if it is a boolean."""
    hello = next(iter(record.inbound("client/hello")), None)
    access = (hello.payload or {}).get("unpaired_access") if hello is not None else None
    enabled = access.get("enabled") if isinstance(access, dict) else None
    return enabled if isinstance(enabled, bool) else None


def not_run(check_ids: Sequence[str], verdict: Verdict, message: str) -> list[Finding]:
    """Return one finding per check of a step that did not run: skipped, or not reached."""
    return [_finding(check_id, verdict, message) for check_id in check_ids]


def activities(payload: dict[str, Any]) -> list[Any]:
    """Return a server/activate payload's `activities`, or an empty list if it has none."""
    listed = payload.get("activities")
    return listed if isinstance(listed, list) else []


def pairing_activations_since_handshake(record: ConnectionRecord, activation: RecordedFrame) -> int:
    """Count the pairing activations sent since the last handshake, up to `activation`."""
    count = 0
    for frame in record.frames:
        if frame.id > activation.id:
            break
        if not frame.inbound and frame.type == "noise/handshake":
            count = 0
        elif is_out(frame, "server/activate") and "pairing" in activities(frame.payload or {}):
            count += 1
    return count


def first_after(
    record: ConnectionRecord, since: int, holds: Callable[[RecordedFrame], bool]
) -> RecordedFrame | None:
    """Return the connection's first frame after frame `since` for which `holds` is true."""
    return next((f for f in record.frames if f.id > since and holds(f)), None)


def first_inbound(
    record: ConnectionRecord, after: RecordedFrame, types: set[str] | frozenset[str] | None
) -> RecordedFrame | None:
    """Return the client's first transport frame after `after`, of one of `types` if given."""
    return first_after(
        record,
        after.id,
        lambda f: (
            f.inbound
            and f.phase == "transport"
            and f.type not in {"client/time"}
            and (types is None or f.type in types)
        ),
    )


def is_in(frame: RecordedFrame, message_type: str) -> bool:
    """Whether `frame` is a `message_type` the client sent over the encrypted transport."""
    return frame.inbound and frame.phase == "transport" and frame.type == message_type


def is_out(frame: RecordedFrame, message_type: str) -> bool:
    """Whether `frame` is a `message_type` the validator sent over the encrypted transport."""
    return not frame.inbound and frame.phase == "transport" and frame.type == message_type


def describe(frame: RecordedFrame) -> str:
    """Return `frame`'s type, with its `reason` when it has one, for a finding's message."""
    reason = (frame.payload or {}).get("reason")
    return f"{frame.type} {reason!r}" if reason is not None else str(frame.type)


def _judge_keying(
    record: ConnectionRecord,
    check_id: str,
    expected: str,
    passed: str,
    *,
    kept: str | None = None,
) -> list[Finding]:
    """
    Judge which PSK keyed the connection's initial handshake against `expected`.

    `kept` replaces the failure message when the session matched the long-term PSK.
    """
    keying = _initial_keying(record)
    message_1 = next(iter(record.outbound("noise/handshake", phase="cleartext")), None)
    if keying is None:
        message_2 = next(iter(record.inbound("noise/handshake", phase="cleartext")), None)
        if (
            message_1 is None
            or record.rejected
            or any(f.phase == "transport" for f in record.frames)
        ):
            return [_not_reached(check_id)]
        if message_2 is None:
            return [
                _finding(
                    check_id,
                    Verdict.FAIL,
                    "the client did not answer Noise message 1, expected to match "
                    + _KEYING_NAMES[expected],
                    message_1,
                )
            ]
        return [
            _finding(
                check_id,
                Verdict.FAIL,
                "the client's Noise message 2 did not verify, expected to match "
                + _KEYING_NAMES[expected],
                message_1,
                message_2,
            )
        ]
    frames = (message_1,) if message_1 is not None else ()
    if keying == expected:
        return [_finding(check_id, Verdict.PASS, passed, *frames)]
    if _REFERENCED[keying] != _REFERENCED[expected]:
        return [
            _finding(
                check_id,
                Verdict.TOOL_ERROR,
                f"the validator referenced {_REFERENCED[keying]}, not {_REFERENCED[expected]}, "
                "so the step tested nothing",
                *frames,
            )
        ]
    message = (
        kept
        if kept is not None and keying == LONG_TERM
        else f"the session was keyed by {_KEYING_NAMES[keying]}, not {_KEYING_NAMES[expected]}"
    )
    return [_finding(check_id, Verdict.FAIL, message, *frames)]


def _initial_keying(record: ConnectionRecord) -> str | None:
    """Return what keyed the connection's initial handshake, or None if none completed."""
    keyed = next((e for e in record.events if e.kind is EventKind.KEYED), None)
    if keyed is None:
        return None
    if any(e.kind is EventKind.CREDENTIAL_MISMATCH for e in record.events):
        return FALLBACK
    return keyed.psk_category


def _judge_refusal(
    record: ConnectionRecord, check_id: str, activation: RecordedFrame, reason: str
) -> list[Finding]:
    """Judge that `activation` was refused with client/goodbye `reason`."""
    response = first_inbound(record, activation, None)
    goodbye = first_inbound(record, activation, {"client/goodbye"})
    if goodbye is not None:
        said = (goodbye.payload or {}).get("reason")
        if said == reason:
            return [
                _finding(
                    check_id,
                    Verdict.PASS,
                    f"the client refused the activation with client/goodbye {reason!r}",
                    activation,
                    goodbye,
                )
            ]
        return [
            _finding(
                check_id,
                Verdict.FAIL,
                f"the client refused the activation with goodbye {said!r}, not {reason!r}",
                activation,
                goodbye,
            )
        ]
    if record.rejected:
        return [_not_reached(check_id)]
    if response is not None:
        return [
            _finding(
                check_id,
                Verdict.FAIL,
                f"the client answered the activation with {describe(response)}, not "
                f"client/goodbye {reason!r}",
                activation,
                response,
            )
        ]
    ending = "closed the connection" if record.closed else "kept the connection open"
    return [
        _finding(
            check_id,
            Verdict.FAIL,
            f"the client {ending} without client/goodbye {reason!r}",
            activation,
        )
    ]


def _judge_abort(record: ConnectionRecord, activation: RecordedFrame) -> list[Finding]:
    """UNPAIRED-004 with unpaired access on: pair/abort method_not_supported, connection kept."""
    response = first_inbound(record, activation, {"pair/abort", "client/goodbye"})
    if response is None or response.type != "pair/abort":
        what = describe(response) if response is not None else "nothing"
        return [
            _finding(
                "UNPAIRED-004",
                Verdict.FAIL,
                f"the client answered with {what}, not pair/abort 'method_not_supported'",
                activation,
                *(response,) if response is not None else (),
            )
        ]
    reason = (response.payload or {}).get("reason")
    if reason != "method_not_supported":
        return [
            _finding(
                "UNPAIRED-004",
                Verdict.FAIL,
                f"the client aborted with {reason!r}, not 'method_not_supported'",
                activation,
                response,
            )
        ]
    if record.closed:
        return [
            _finding(
                "UNPAIRED-004",
                Verdict.FAIL,
                "the client aborted with 'method_not_supported' but closed the connection",
                activation,
                response,
            )
        ]
    return [
        _finding(
            "UNPAIRED-004",
            Verdict.PASS,
            "the client aborted with 'method_not_supported' and kept the connection",
            activation,
            response,
        )
    ]


def _first_outbound_activation(
    record: ConnectionRecord, holds: Callable[[dict[str, Any]], bool]
) -> RecordedFrame | None:
    return next((f for f in record.outbound("server/activate") if holds(f.payload or {})), None)


def _pairing_activation(record: ConnectionRecord, since: int) -> RecordedFrame | None:
    """Return the first pairing_psk activation the validator sent after frame `since`."""
    return first_after(
        record,
        since,
        lambda f: (
            is_out(f, "server/activate")
            and "pairing" in activities(f.payload or {})
            and ((f.payload or {}).get("pairing") or {}).get("method") == "pairing_psk"
        ),
    )


def _output_delay(frame: RecordedFrame) -> int | None:
    player = (frame.payload or {}).get("player")
    delay = player.get("output_delay_ms") if isinstance(player, dict) else None
    return delay if isinstance(delay, int) and not isinstance(delay, bool) else None


def _finding(check_id: str, verdict: Verdict, message: str, *frames: RecordedFrame) -> Finding:
    return Finding(
        check_id, verdict, SPEC_REFS[check_id], message, frames=tuple(f.id for f in frames)
    )


def _n_a(check_id: str, message: str) -> Finding:
    return _finding(check_id, Verdict.NOT_APPLICABLE, message)


def _not_reached(check_id: str) -> Finding:
    return _n_a(check_id, "not reached: the connection ended at an earlier failure")
