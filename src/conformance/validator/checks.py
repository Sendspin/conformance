"""
Machine checks over the frames of one recorded connection.

Each check is a pure function of a `ConnectionRecord` that returns its findings.
References are to the Sendspin spec at tag `1.0.0-rc1`; `M:` lines are
`messaging.md`. A check whose evidence never arrived, because the connection ended
at an earlier failure, is N/A rather than FAIL or PASS.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .findings import Finding, Verdict
from .noncompliance_refs import UnmappedReasonError, spec_ref_for
from .recorder import RecordedFrame, RecordedNoncompliance

SPEC_REFS = {
    "CORE-INIT-001": "messaging.md:5, messaging.md:16",
    "CORE-INIT-002": "messaging.md:145",
    "CORE-INIT-003": "messaging.md:146",
    "CORE-INIT-004": "messaging.md:147, connection.md:78-81",
    "CORE-INIT-005": "messaging.md:28, messaging.md:141-147",
    "CORE-NOISE-001": "messaging.md:171",
    "CORE-ORDER-001": "messaging.md:14",
    "CORE-HELLO-001": "messaging.md:11, messaging.md:198",
    "CORE-HELLO-002": "messaging.md:216-218, messaging.md:225",
    "CORE-HELLO-003": "template.md:124, template.md:146",
    "CORE-HELLO-004": "messaging.md:219, pairing.md:305-319",
    "CORE-HELLO-005": "messaging.md:220-221",
    "CORE-HELLO-006": "messaging.md:28, messaging.md:202-223",
    "CORE-HELLO-007": "connection.md:19",
    "CORE-STATE-001": "messaging.md:297-299",
    "CORE-STATE-002": "messaging.md:301-303",
    "CORE-STATE-003": "messaging.md:306-309, template.md:134",
    "CORE-TIME-001": "messaging.md:281",
}
STRICT_CHECK_ID = "CORE-STRICT-001"
RECORDER_CHECK_ID = "CORE-RECORDER-001"
# The strict server's reason when Noise message 2's payload is wrong, CORE-NOISE-001's
# only evidence, since the payload is encrypted.
NOISE_PAYLOAD_REASON = "Noise message 2 payload is not the literal bytes {}"

logger = logging.getLogger(__name__)

SUITES = frozenset({"25519_ChaChaPoly_SHA256", "25519_AESGCM_SHA256"})
# Role versions whose support object client/hello must carry when they are listed.
SUPPORT_OBJECT_ROLES = frozenset({"player@v1", "source@v1", "visualizer@v1"})
# client/state objects, keyed by role family.
STATE_OBJECT_ROLES = frozenset({"player", "source", "artwork", "visualizer"})
HELLO_FIELDS = frozenset(
    {"name", "device_info", "supported_roles", "supported_pair_methods", "unpaired_access"}
)
DEVICE_INFO_FIELDS = frozenset({"product_name", "manufacturer", "software_version", "mac_address"})
CODE_PAIR_METHODS = frozenset({"static_pairing_code", "dynamic_pairing_code"})
PAIR_METHOD_FIELDS = {
    "pairing_psk": frozenset({"locations"}),
    "static_pairing_code": frozenset({"locations"}),
    "dynamic_pairing_code": frozenset({"out_channels", "formats"}),
}
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{43}")
_ROLE = re.compile(r"[^@\s]+@[^@\s]+")
_SUPPORT_SUFFIX = "_support"


@dataclass(frozen=True, slots=True)
class ConnectionRecord:
    """Everything recorded on one connection, in recording order."""

    number: int
    frames: tuple[RecordedFrame, ...]
    noncompliance: tuple[RecordedNoncompliance, ...] = ()

    def inbound(self, message_type: str, *, phase: str = "transport") -> list[RecordedFrame]:
        """Return the client's frames of `message_type` in `phase`."""
        return [f for f in self.frames if f.inbound and f.phase == phase and f.type == message_type]

    def outbound(self, message_type: str, *, phase: str = "transport") -> list[RecordedFrame]:
        """Return the validator's frames of `message_type` in `phase`."""
        return [
            f for f in self.frames if not f.inbound and f.phase == phase and f.type == message_type
        ]

    @classmethod
    def of(
        cls,
        number: int,
        frames: Iterable[RecordedFrame],
        noncompliance: Iterable[RecordedNoncompliance],
    ) -> ConnectionRecord:
        """Return what a session recorded on connection `number`."""
        return cls(
            number,
            tuple(f for f in frames if f.connection == number),
            tuple(o for o in noncompliance if o.connection == number),
        )

    @property
    def rejected(self) -> bool:
        """Whether the strict server flagged a violation, which ends the connection."""
        return bool(self.noncompliance)


Check = Callable[[ConnectionRecord], list[Finding]]


def judge_session(
    frames: Sequence[RecordedFrame],
    noncompliance: Sequence[RecordedNoncompliance],
    tool_errors: Sequence[str],
    *,
    connection: int,
    txt_name: str | None = None,
) -> list[Finding]:
    """
    Return every finding for the observed `connection` of a recorded session.

    Frames and violations of any other connection belong to other clients and are
    not judged. A frame the recorder could not process is a TOOL_ERROR, since any
    check may have missed it.
    """
    record = ConnectionRecord.of(connection, frames, noncompliance)
    return [
        *run_checks(record, txt_name=txt_name),
        *check_noncompliance(record.noncompliance),
        *(Finding(RECORDER_CHECK_ID, Verdict.TOOL_ERROR, "-", error) for error in tool_errors),
    ]


def run_checks(connection: ConnectionRecord, *, txt_name: str | None = None) -> list[Finding]:
    """
    Run every check on `connection` and return their findings, in check order.

    `txt_name` is the TXT `name` the client advertised over mDNS, when the validator
    discovered it. A check that raises reports TOOL_ERROR instead of a verdict.
    """
    checks = {
        **CHECKS,
        "CORE-HELLO-007": lambda record: check_hello_name_matches_txt(record, txt_name),
    }
    findings: list[Finding] = []
    for check_id, check in checks.items():
        try:
            findings.extend(check(connection))
        except Exception as exc:  # noqa: BLE001 - a check bug must surface, never pass silently
            logger.exception("Check %s failed", check_id)
            findings.append(
                _finding(
                    check_id,
                    Verdict.TOOL_ERROR,
                    f"the check failed: {type(exc).__name__}: {exc}",
                )
            )
    return findings


def check_noncompliance(noncompliance: Sequence[RecordedNoncompliance]) -> list[Finding]:
    """
    Return a finding for every violation the strict server flagged, or one PASS if none.

    The strict server rejects a client at its first violation, so this is the verdict
    of every rule aiosendspin enforces, including those no other check covers. A
    violation CORE-NOISE-001 already reports is left to it. A
    rejection for something spec 1.0.0-rc1 does not forbid, or for a reason with no
    known spec reference, is the validator's TOOL_ERROR, never the client's FAIL.
    """
    if not noncompliance:
        return [
            Finding(
                STRICT_CHECK_ID,
                Verdict.PASS,
                "-",
                "aiosendspin's strict mode accepted every message",
            )
        ]
    return [
        _strict_finding(observation)
        for observation in noncompliance
        if observation.reason != NOISE_PAYLOAD_REASON
    ]


def check_opens_with_client_init(connection: ConnectionRecord) -> list[Finding]:
    """CORE-INIT-001: the client opens the connection with a client/init text message."""
    first = connection.frames[0]
    if _client_init(connection) is None:
        return [
            _finding(
                "CORE-INIT-001",
                Verdict.FAIL,
                f"the client's first frame was not a client/init message ({_what(first)})",
                first,
            )
        ]
    return [_finding("CORE-INIT-001", Verdict.PASS, "the client opened with client/init", first)]


def check_client_id(connection: ConnectionRecord) -> list[Finding]:
    """CORE-INIT-002: client_id is 43 characters of base64url without padding."""
    return _judge_client_init(
        connection,
        "CORE-INIT-002",
        lambda payload: _match(payload.get("client_id"), _CLIENT_ID),
        "client_id is 43 characters of base64url without padding",
        lambda payload: (
            f"client_id {payload.get('client_id')!r} is not 43 characters of "
            "base64url without padding"
        ),
    )


def check_init_version(connection: ConnectionRecord) -> list[Finding]:
    """CORE-INIT-003: version is the integer 1."""
    return _judge_client_init(
        connection,
        "CORE-INIT-003",
        lambda payload: _is_int(payload.get("version")) and payload["version"] == 1,
        "version is 1",
        lambda payload: f"version is {payload.get('version')!r}, not the integer 1",
    )


def check_init_suite(connection: ConnectionRecord) -> list[Finding]:
    """CORE-INIT-004: suite is one of the two defined Noise suites."""
    return _judge_client_init(
        connection,
        "CORE-INIT-004",
        lambda payload: payload.get("suite") in SUITES,
        "suite is a defined Noise suite",
        lambda payload: f"suite {payload.get('suite')!r} is not one of {', '.join(sorted(SUITES))}",
    )


def check_init_fields(connection: ConnectionRecord) -> list[Finding]:
    """CORE-INIT-005: client/init carries no field the spec does not define."""
    init = _client_init(connection)
    if init is None:
        return [_no_client_init("CORE-INIT-005")]
    assert init.message is not None
    undefined = _undefined(init.message, {"type", "payload"})
    undefined += [
        f"payload.{key}"
        for key in _undefined(init.payload or {}, {"client_id", "version", "suite"})
    ]
    if undefined:
        return [
            _finding(
                "CORE-INIT-005",
                Verdict.FAIL,
                f"client/init carries undefined fields: {', '.join(undefined)}",
                init,
            )
        ]
    return [_finding("CORE-INIT-005", Verdict.PASS, "client/init has no undefined fields", init)]


def check_noise_message_2(connection: ConnectionRecord) -> list[Finding]:
    """
    CORE-NOISE-001: the inner payload of Noise message 2 is exactly `{}`.

    The payload is encrypted, so the verdict comes from the strict server, which
    checks it before it completes the handshake.
    """
    message_2 = next(iter(connection.inbound("noise/handshake", phase="cleartext")), None)
    rejection = next(
        (o for o in connection.noncompliance if o.reason == NOISE_PAYLOAD_REASON),
        None,
    )
    if message_2 is None:
        if connection.outbound("server/error", phase="cleartext"):
            return [
                _finding(
                    "CORE-NOISE-001",
                    Verdict.NOT_APPLICABLE,
                    "the validator answered the opening frame with server/error",
                )
            ]
        return [
            _finding(
                "CORE-NOISE-001",
                Verdict.TOOL_ERROR,
                "the client sent no Noise message 2, so its payload was not seen",
            )
        ]
    if rejection is not None:
        return [_finding("CORE-NOISE-001", Verdict.FAIL, rejection.reason, message_2)]
    if any(f.phase == "transport" and not f.inbound for f in connection.frames):
        return [
            _finding(
                "CORE-NOISE-001",
                Verdict.PASS,
                "Noise message 2 carried {} and the handshake completed",
                message_2,
            )
        ]
    return [
        _finding(
            "CORE-NOISE-001",
            Verdict.TOOL_ERROR,
            "the handshake ended after Noise message 2 for a reason the validator cannot see",
            message_2,
        )
    ]


def check_nothing_before_activation(connection: ConnectionRecord) -> list[Finding]:
    """CORE-ORDER-001: the client sends only client/hello or client/goodbye before activation."""
    transport = [f for f in connection.frames if f.phase == "transport"]
    if not transport:
        return [_finding("CORE-ORDER-001", Verdict.NOT_APPLICABLE, "the handshake never completed")]
    activation = next(
        (i for i, f in enumerate(transport) if not f.inbound and f.type == "server/activate"),
        len(transport),
    )
    early = [
        f
        for f in transport[:activation]
        if f.inbound and f.type not in {"client/hello", "client/goodbye"}
    ]
    if early:
        return [
            _finding(
                "CORE-ORDER-001",
                Verdict.FAIL,
                "the client sent "
                + ", ".join(_what(f) for f in early)
                + " before the initial server/activate",
                *early,
            )
        ]
    return [
        _finding(
            "CORE-ORDER-001",
            Verdict.PASS,
            "the client sent nothing but its hello before activation",
        )
    ]


def check_hello_sent(connection: ConnectionRecord) -> list[Finding]:
    """CORE-HELLO-001: the client answers server/hello with client/hello."""
    server_hello = next(iter(connection.outbound("server/hello")), None)
    client_hello = next(iter(connection.inbound("client/hello")), None)
    if client_hello is not None:
        return [
            _finding("CORE-HELLO-001", Verdict.PASS, "the client sent client/hello", client_hello)
        ]
    if server_hello is None:
        return [
            _finding("CORE-HELLO-001", Verdict.NOT_APPLICABLE, "the validator sent no server/hello")
        ]
    goodbye = next(iter(connection.inbound("client/goodbye")), None)
    if goodbye is not None:
        return [
            _finding(
                "CORE-HELLO-001",
                Verdict.NOT_APPLICABLE,
                "the client said goodbye before its hello",
                goodbye,
            )
        ]
    if connection.rejected:
        return [_not_reached("CORE-HELLO-001")]
    return [
        _finding(
            "CORE-HELLO-001",
            Verdict.FAIL,
            "the client never answered server/hello with client/hello",
            server_hello,
        )
    ]


def check_hello_support_objects(connection: ConnectionRecord) -> list[Finding]:
    """CORE-HELLO-002: a support object is sent for, and only for, listed role versions."""
    hello = _client_hello(connection)
    if hello is None:
        return [_no_client_hello("CORE-HELLO-002")]
    payload = hello.payload or {}
    roles = _roles(payload)
    problems = [
        f"{role} is listed without its {role}{_SUPPORT_SUFFIX} object"
        for role in sorted(SUPPORT_OBJECT_ROLES & set(roles))
        if not isinstance(payload.get(f"{role}{_SUPPORT_SUFFIX}"), dict)
    ]
    problems += [
        f"{key} is sent for a role version not in supported_roles"
        for key in sorted(payload)
        if key.endswith(_SUPPORT_SUFFIX)
        and "@" in key
        and key.removesuffix(_SUPPORT_SUFFIX) not in roles
    ]
    if problems:
        return [_finding("CORE-HELLO-002", Verdict.FAIL, "; ".join(problems), hello)]
    return [
        _finding(
            "CORE-HELLO-002",
            Verdict.PASS,
            "every listed role version has the support object it requires",
            hello,
        )
    ]


def check_hello_roles(connection: ConnectionRecord) -> list[Finding]:
    """CORE-HELLO-003: supported_roles is a list of `<role>@<version>` strings."""
    hello = _client_hello(connection)
    if hello is None:
        return [_no_client_hello("CORE-HELLO-003")]
    roles = (hello.payload or {}).get("supported_roles")
    if not isinstance(roles, list):
        return [
            _finding(
                "CORE-HELLO-003",
                Verdict.FAIL,
                f"supported_roles is {_kind(roles)}, not a list",
                hello,
            )
        ]
    malformed = [role for role in roles if not _match(role, _ROLE)]
    if malformed:
        return [
            _finding(
                "CORE-HELLO-003",
                Verdict.FAIL,
                f"roles not of the form <role>@<version>: {', '.join(map(repr, malformed))}",
                hello,
            )
        ]
    return [_finding("CORE-HELLO-003", Verdict.PASS, "every role is <role>@<version>", hello)]


def check_hello_pair_methods(connection: ConnectionRecord) -> list[Finding]:
    """
    CORE-HELLO-004: supported_pair_methods offers pairing_psk and at most one code method.

    Each recognized method's descriptor must be well formed. Method keys the spec does
    not define are reserved, and the server ignores them, so they are not judged.
    """
    hello = _client_hello(connection)
    if hello is None:
        return [_no_client_hello("CORE-HELLO-004")]
    methods = (hello.payload or {}).get("supported_pair_methods")
    if not isinstance(methods, dict):
        return [
            _finding(
                "CORE-HELLO-004",
                Verdict.FAIL,
                f"supported_pair_methods is {_kind(methods)}, not an object",
                hello,
            )
        ]
    problems = []
    if "pairing_psk" not in methods:
        problems.append("pairing_psk is not offered")
    if len(CODE_PAIR_METHODS & methods.keys()) > 1:
        problems.append("both static_pairing_code and dynamic_pairing_code are offered")
    for method in sorted(PAIR_METHOD_FIELDS.keys() & methods.keys()):
        problems += _descriptor_problems(method, methods[method])
    if problems:
        return [_finding("CORE-HELLO-004", Verdict.FAIL, "; ".join(problems), hello)]
    return [
        _finding(
            "CORE-HELLO-004",
            Verdict.PASS,
            f"pairing methods are well formed: {', '.join(sorted(methods))}",
            hello,
        )
    ]


def check_hello_unpaired_access(connection: ConnectionRecord) -> list[Finding]:
    """CORE-HELLO-005: unpaired_access is present with a boolean `enabled`."""
    hello = _client_hello(connection)
    if hello is None:
        return [_no_client_hello("CORE-HELLO-005")]
    access = (hello.payload or {}).get("unpaired_access")
    if not isinstance(access, dict) or not isinstance(access.get("enabled"), bool):
        return [
            _finding(
                "CORE-HELLO-005",
                Verdict.FAIL,
                f"unpaired_access is {access!r}, not an object with a boolean enabled",
                hello,
            )
        ]
    return [
        _finding(
            "CORE-HELLO-005",
            Verdict.PASS,
            f"unpaired_access.enabled is {str(access['enabled']).lower()}",
            hello,
        )
    ]


def check_hello_fields(connection: ConnectionRecord) -> list[Finding]:
    """
    CORE-HELLO-006: client/hello carries no undefined field that is not `_`-prefixed.

    Support objects count as defined for the role versions the spec gives one and for
    listed application-specific role versions, whose name or version starts with `_`.
    """
    hello = _client_hello(connection)
    if hello is None:
        return [_no_client_hello("CORE-HELLO-006")]
    assert hello.message is not None
    payload = hello.payload or {}
    custom_support = {
        f"{role}{_SUPPORT_SUFFIX}"
        for role in _roles(payload)
        if role.startswith("_") or role.partition("@")[2].startswith("_")
    }
    defined = (
        HELLO_FIELDS
        | {f"{role}{_SUPPORT_SUFFIX}" for role in SUPPORT_OBJECT_ROLES}
        | custom_support
    )
    undefined = _undefined(hello.message, {"type", "payload"})
    undefined += [f"payload.{key}" for key in _undefined(payload, defined, custom=True)]
    for name, fields in (("device_info", DEVICE_INFO_FIELDS), ("unpaired_access", {"enabled"})):
        if isinstance(payload.get(name), dict):
            undefined += [f"payload.{name}.{key}" for key in _undefined(payload[name], fields)]
    if undefined:
        return [
            _finding(
                "CORE-HELLO-006",
                Verdict.FAIL,
                f"client/hello carries undefined fields: {', '.join(undefined)}",
                hello,
            )
        ]
    return [_finding("CORE-HELLO-006", Verdict.PASS, "client/hello has no undefined fields", hello)]


def check_hello_name_matches_txt(
    connection: ConnectionRecord, txt_name: str | None
) -> list[Finding]:
    """CORE-HELLO-007: the client/hello name matches the mDNS TXT `name`, a SHOULD."""
    if txt_name is None:
        return [
            _finding(
                "CORE-HELLO-007",
                Verdict.NOT_APPLICABLE,
                "the client was not discovered over mDNS with a TXT name",
            )
        ]
    hello = _client_hello(connection)
    if hello is None:
        return [_no_client_hello("CORE-HELLO-007")]
    name = (hello.payload or {}).get("name")
    if name != txt_name:
        return [
            _finding(
                "CORE-HELLO-007",
                Verdict.WARN,
                f"client/hello name {name!r} differs from TXT name {txt_name!r}",
                hello,
            )
        ]
    return [
        _finding("CORE-HELLO-007", Verdict.PASS, "client/hello name matches the TXT name", hello)
    ]


def check_initial_state_sent(connection: ConnectionRecord) -> list[Finding]:
    """CORE-STATE-001: a client with active roles sends client/state after activation."""
    activation = next(iter(connection.outbound("server/activate")), None)
    if activation is None:
        return [
            _finding(
                "CORE-STATE-001", Verdict.NOT_APPLICABLE, "the validator never activated the client"
            )
        ]
    roles = _active_roles(connection, before=_initial_state(connection))
    if not roles:
        return [
            _finding(
                "CORE-STATE-001",
                Verdict.NOT_APPLICABLE,
                "no roles are active, so no initial client/state is owed",
                activation,
            )
        ]
    state = _initial_state(connection)
    if state is not None:
        return [
            _finding(
                "CORE-STATE-001",
                Verdict.PASS,
                "the client sent client/state after activation",
                activation,
                state,
            )
        ]
    goodbye = next((f for f in connection.inbound("client/goodbye") if f.id > activation.id), None)
    if goodbye is not None:
        return [
            _finding(
                "CORE-STATE-001",
                Verdict.NOT_APPLICABLE,
                "the client said goodbye before reporting its state",
                goodbye,
            )
        ]
    if connection.rejected:
        return [_not_reached("CORE-STATE-001")]
    return [
        _finding(
            "CORE-STATE-001",
            Verdict.FAIL,
            f"no client/state followed activating {', '.join(sorted(roles))}",
            activation,
        )
    ]


def check_state_available(connection: ConnectionRecord) -> list[Finding]:
    """CORE-STATE-002: every client/state carries a boolean `available`."""
    states = connection.inbound("client/state")
    if not states:
        return [_finding("CORE-STATE-002", Verdict.NOT_APPLICABLE, "no client/state was sent")]
    missing = [f for f in states if not isinstance((f.payload or {}).get("available"), bool)]
    if missing:
        return [
            _finding(
                "CORE-STATE-002", Verdict.FAIL, "client/state without a boolean available", *missing
            )
        ]
    return [
        _finding(
            "CORE-STATE-002",
            Verdict.PASS,
            f"all {len(states)} client/state messages carry available",
            *states[:1],
        )
    ]


def check_state_role_objects(connection: ConnectionRecord) -> list[Finding]:
    """
    CORE-STATE-003: the initial client/state carries objects for active roles only.

    The state is judged against every role activated before it arrived, so a removal
    crossing it on the wire is never counted against the client.
    """
    state = _initial_state(connection)
    if state is None:
        return [_finding("CORE-STATE-003", Verdict.NOT_APPLICABLE, "no client/state was sent")]
    families = {role.partition("@")[0] for role in _active_roles(connection, before=state)}
    inactive = sorted((STATE_OBJECT_ROLES - families) & (state.payload or {}).keys())
    if inactive:
        return [
            _finding(
                "CORE-STATE-003",
                Verdict.FAIL,
                f"client/state carries objects for inactive roles: {', '.join(inactive)}",
                state,
            )
        ]
    return [
        _finding(
            "CORE-STATE-003",
            Verdict.PASS,
            "client/state carries objects for active roles only",
            state,
        )
    ]


def check_time_integer(connection: ConnectionRecord) -> list[Finding]:
    """CORE-TIME-001: every client/time carries an integer `client_transmitted`."""
    times = connection.inbound("client/time")
    if not times:
        return [_finding("CORE-TIME-001", Verdict.NOT_APPLICABLE, "no client/time was sent")]
    bad = [f for f in times if not _is_int((f.payload or {}).get("client_transmitted"))]
    if bad:
        return [
            _finding(
                "CORE-TIME-001",
                Verdict.FAIL,
                "client/time without an integer client_transmitted",
                *bad,
            )
        ]
    return [
        _finding(
            "CORE-TIME-001",
            Verdict.PASS,
            f"all {len(times)} client/time messages carry an integer",
            *times[:1],
        )
    ]


CHECKS: dict[str, Check] = {
    "CORE-INIT-001": check_opens_with_client_init,
    "CORE-INIT-002": check_client_id,
    "CORE-INIT-003": check_init_version,
    "CORE-INIT-004": check_init_suite,
    "CORE-INIT-005": check_init_fields,
    "CORE-NOISE-001": check_noise_message_2,
    "CORE-ORDER-001": check_nothing_before_activation,
    "CORE-HELLO-001": check_hello_sent,
    "CORE-HELLO-002": check_hello_support_objects,
    "CORE-HELLO-003": check_hello_roles,
    "CORE-HELLO-004": check_hello_pair_methods,
    "CORE-HELLO-005": check_hello_unpaired_access,
    "CORE-HELLO-006": check_hello_fields,
    # run_checks replaces this with the TXT name bound, keeping its place in the order.
    "CORE-HELLO-007": lambda connection: check_hello_name_matches_txt(connection, None),
    "CORE-STATE-001": check_initial_state_sent,
    "CORE-STATE-002": check_state_available,
    "CORE-STATE-003": check_state_role_objects,
    "CORE-TIME-001": check_time_integer,
}


def _finding(check_id: str, verdict: Verdict, message: str, *frames: RecordedFrame) -> Finding:
    return Finding(
        check_id, verdict, SPEC_REFS[check_id], message, frames=tuple(f.id for f in frames)
    )


def _strict_finding(observation: RecordedNoncompliance) -> Finding:
    frames = (observation.after_frame,) if observation.after_frame is not None else ()
    try:
        spec_ref = spec_ref_for(observation.reason)
    except UnmappedReasonError:
        return Finding(
            STRICT_CHECK_ID,
            Verdict.TOOL_ERROR,
            "-",
            f"aiosendspin rejected the client for a reason with no known spec reference: "
            f"{observation.reason}",
            frames=frames,
        )
    if spec_ref is None:
        return Finding(
            STRICT_CHECK_ID,
            Verdict.TOOL_ERROR,
            "-",
            f"aiosendspin rejected the client for something spec 1.0.0-rc1 does not forbid: "
            f"{observation.reason}",
            frames=frames,
        )
    return Finding(
        STRICT_CHECK_ID,
        Verdict.FAIL,
        spec_ref,
        f"aiosendspin rejected the client: {observation.reason}",
        frames=frames,
    )


def _not_reached(check_id: str) -> Finding:
    return _finding(
        check_id, Verdict.NOT_APPLICABLE, "not reached: the connection ended at an earlier failure"
    )


def _client_init(connection: ConnectionRecord) -> RecordedFrame | None:
    first = connection.frames[0]
    if first.inbound and first.phase == "cleartext" and first.type == "client/init":
        return first
    return None


def _no_client_init(check_id: str) -> Finding:
    return _finding(check_id, Verdict.NOT_APPLICABLE, "no client/init to judge")


def _judge_client_init(
    connection: ConnectionRecord,
    check_id: str,
    holds: Callable[[dict[str, Any]], bool],
    passed: str,
    failed: Callable[[dict[str, Any]], str],
) -> list[Finding]:
    init = _client_init(connection)
    if init is None:
        return [_no_client_init(check_id)]
    payload = init.payload or {}
    if holds(payload):
        return [_finding(check_id, Verdict.PASS, passed, init)]
    return [_finding(check_id, Verdict.FAIL, failed(payload), init)]


def _client_hello(connection: ConnectionRecord) -> RecordedFrame | None:
    return next(iter(connection.inbound("client/hello")), None)


def _no_client_hello(check_id: str) -> Finding:
    return _finding(check_id, Verdict.NOT_APPLICABLE, "no client/hello to judge")


def _initial_state(connection: ConnectionRecord) -> RecordedFrame | None:
    return next(iter(connection.inbound("client/state")), None)


def _active_roles(connection: ConnectionRecord, *, before: RecordedFrame | None) -> set[str]:
    """Return every role activated before `before`, or ever when it is None."""
    roles: set[str] = set()
    for activation in connection.outbound("server/activate"):
        if before is not None and activation.id > before.id:
            break
        active = (activation.payload or {}).get("active_roles") or []
        roles.update(role for role in active if isinstance(role, str))
    return roles


def _roles(payload: dict[str, Any]) -> list[str]:
    roles = payload.get("supported_roles")
    if not isinstance(roles, list):
        return []
    return [role for role in roles if isinstance(role, str)]


def _descriptor_problems(method: str, descriptor: object) -> list[str]:
    if not isinstance(descriptor, dict):
        return [f"{method} descriptor is {_kind(descriptor)}, not an object"]
    problems = [
        f"{method}.{key} is not a defined descriptor field"
        for key in _undefined(descriptor, PAIR_METHOD_FIELDS[method])
    ]
    required = PAIR_METHOD_FIELDS[method] if method == "dynamic_pairing_code" else set()
    for field in sorted(PAIR_METHOD_FIELDS[method]):
        if field not in descriptor:
            if field in required:
                problems.append(f"{method}.{field} is missing")
            continue
        values = descriptor[field]
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            problems.append(f"{method}.{field} is not a list of strings")
    if method == "dynamic_pairing_code" and isinstance(descriptor.get("formats"), list):
        formats = descriptor["formats"]
        if not formats:
            problems.append(f"{method}.formats is empty")
        channels = descriptor.get("out_channels")
        if "qr_code" in formats and not (isinstance(channels, list) and "display" in channels):
            problems.append(f"{method} offers qr_code without a display out_channel")
    return problems


def _undefined(obj: dict[str, Any], defined: Iterable[str], *, custom: bool = False) -> list[str]:
    """Return `obj`'s undefined keys, sorted; `custom` admits `_`-prefixed ones."""
    allowed = set(defined)
    return sorted(key for key in obj if key not in allowed and not (custom and key.startswith("_")))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _match(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _kind(value: object) -> str:
    return "missing" if value is None else f"a {type(value).__name__}"


def _what(frame: RecordedFrame) -> str:
    if frame.type is not None:
        return frame.type
    if frame.error is not None:
        return f"a frame that did not decode: {frame.error}"
    if frame.binary_type is not None:
        return f"a binary message of type {frame.binary_type}"
    return "a JSON message without a type"
