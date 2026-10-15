"""
Machine and human checks for pairing with a code: steps P2, P3 and P4.

Each machine check is a pure function of the `ConnectionRecord` of the connection
its step ran on, judged from frame `since`, the last one recorded before the step
began. References are to the Sendspin spec at tag `1.0.0-rc1`. The server verifies
`client/pair-confirm` inside aiosendspin, which ends a failed attempt with an
exception and no frame, so those checks read the PAIRING_FAILED event the step
recorded from it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import IntEnum

from conformance._vendor.aiosendspin.noise.pairing_token import (
    decode_pairing_code_token,
    encode_pairing_code_token,
)

from .checks import ConnectionRecord
from .findings import Evidence, Finding, Verdict
from .recorder import EventKind, RecordedFrame, redacted_length
from .suite_checks import (
    activities,
    describe,
    first_after,
    first_inbound,
    is_in,
    is_out,
    pairing_activations_since_handshake,
)

# The check id prefix of each walkthrough; QR-format dynamic pairing has its own ids.
DYNAMIC, DYNAMIC_QR, STATIC, CONTROL = "PAIR-DYN", "PAIR-DYN-QR", "PAIR-STATIC", "PAIR-CTL"
_DYNAMIC_REFS = {
    "001": "pairing.md:122, pairing.md:147, pairing.md:341",
    "002": "pairing.md:327, pairing.md:333, pairing.md:340",
    "003": "pairing.md:284-289, pairing.md:299, pairing.md:382, pairing.md:391",
    "004": "pairing.md:171, pairing.md:183-185, pairing.md:375",
    "005": "pairing.md:173-175, pairing.md:179",
    "006": "pairing.md:176, pairing.md:179, pairing.md:303",
    "007": "pairing.md:177, pairing.md:179, pairing.md:303",
    "008": "pairing.md:3, pairing.md:179, pairing.md:299, pairing.md:391",
}
SPEC_REFS = {
    **{f"{DYNAMIC}-{n}": ref for n, ref in _DYNAMIC_REFS.items()},
    **{f"{DYNAMIC_QR}-{n}": ref for n, ref in _DYNAMIC_REFS.items()},
    "PAIR-DYN-009": "pairing.md:242",
    "PAIR-DYN-010": "pairing.md:171",
    "PAIR-DYN-011": "pairing.md:161, pairing.md:314",
    "PAIR-DYN-012": "pairing.md:161, messaging.md:193",
    "PAIR-DYN-QR-009": "pairing.md:163, pairing.md:262-268",
    "PAIR-STATIC-001": "pairing.md:232, pairing.md:238, pairing.md:331",
    "PAIR-STATIC-002": "pairing.md:327, pairing.md:333, pairing.md:340",
    "PAIR-STATIC-003": "pairing.md:323, pairing.md:341, pairing.md:382",
    "PAIR-STATIC-004": "pairing.md:238, pairing.md:338",
    "PAIR-STATIC-005": "pairing.md:284-289, pairing.md:299, pairing.md:391",
    "PAIR-STATIC-006": "pairing.md:224, pairing.md:226",
    "PAIR-STATIC-007": "pairing.md:3, pairing.md:226, pairing.md:299, pairing.md:391",
    "PAIR-STATIC-008": "pairing.md:242",
    "PAIR-STATIC-009": "pairing.md:224, pairing.md:230",
    "PAIR-STATIC-010": "pairing.md:230, pairing.md:232, pairing.md:238",
    "PAIR-CTL-001": "pairing.md:39-43, pairing.md:401, pairing.md:408",
    "PAIR-CTL-002": "pairing.md:35, pairing.md:404",
}
# The base64url length of each fixed-size field the client sends in a code flow.
_CLIENT_FIELD_LENGTHS = {
    ("client/pair-auth", "pake_msg_2"): 43,
    ("client/pair-confirm", "client_kc"): 86,
    ("client/pair-confirm", "wrapped_nonce_B"): 64,
    ("client/pair-finalize", "wrapped_psk"): 64,
}
# The base64url length of commit_B, a SHA-256 digest.
_COMMIT_LENGTH = 43
_PAIRING_STARTS = frozenset({"client/pair-init", "client/pair-pending"})
_REFUSALS = frozenset({"pair/abort", "client/goodbye"})
# How many failed attempts close a static pairing window.
LOCKOUT_ATTEMPTS = 5


class Stage(IntEnum):
    """The server's checks of `client/pair-confirm` and what follows, in spec order."""

    CLIENT_KC = 0
    COMMIT = 1
    BINDING = 2
    UNWRAP = 3


# aiosendspin's reasons for failing an attempt once `client/pair-confirm` arrived, by
# the stage that failed. They are matched against the PAIRING_FAILED event's detail, so
# a test fails when the bundled copy gains a reason missing here.
FAILURE_STAGES: dict[str, Stage] = {
    "malformed client_kc": Stage.CLIENT_KC,
    "pairing_code_mismatch": Stage.CLIENT_KC,
    "client/pair-confirm carries wrapped_nonce_B": Stage.CLIENT_KC,
    "client/pair-confirm missing wrapped_nonce_B": Stage.COMMIT,
    "client/pair-confirm must carry only nonce_B": Stage.COMMIT,
    "malformed nonce_B": Stage.COMMIT,
    "malformed wrapped_nonce_B": Stage.COMMIT,
    "revealed nonce_B does not match commit_B": Stage.COMMIT,
    "entered pairing code is not bound to this connection": Stage.BINDING,
    "ClientPairFinalizeMessage": Stage.UNWRAP,
    "client/pair-finalize": Stage.UNWRAP,
    "malformed wrapped_psk": Stage.UNWRAP,
    "malformed long_term_psk": Stage.UNWRAP,
    "did not arrive in time": Stage.UNWRAP,
    "completing the stored pairing record timed out": Stage.UNWRAP,
}
_STAGE_PASSED = {
    Stage.CLIENT_KC: "client_kc verified under the entered code",
    Stage.COMMIT: "wrapped_nonce_B unwrapped to a nonce_B that opens commit_B",
    Stage.BINDING: "the code derived from h, nonce_A and nonce_B is the entered code",
    Stage.UNWRAP: "wrapped_psk unwrapped and the client re-handshook to that long-term PSK",
}


def check_commit(record: ConnectionRecord, since: int, prefix: str) -> list[Finding]:
    """`<prefix>-001`: the client's client/pair-init carries a 43-character commit_B."""
    check_id = f"{prefix}-001"
    pair_init = first_after(record, since, lambda f: is_in(f, "client/pair-init"))
    if pair_init is None:
        return [_unanswered(record, check_id, "client/pair-init")]
    commit = (pair_init.payload or {}).get("commit_B")
    if not isinstance(commit, str) or len(commit) != _COMMIT_LENGTH:
        what = "no commit_B" if commit is None else "a commit_B that is not 43 characters"
        return [_finding(check_id, Verdict.FAIL, f"client/pair-init carries {what}", pair_init)]
    server_init = first_after(record, since, lambda f: is_out(f, "server/pair-init"))
    if server_init is not None and server_init.id < pair_init.id:
        return [
            _finding(
                check_id,
                Verdict.TOOL_ERROR,
                "the validator sent server/pair-init before the client committed",
                server_init,
            )
        ]
    return [
        _finding(
            check_id,
            Verdict.PASS,
            "client/pair-init committed to nonce_B with commit_B before server/pair-init",
            pair_init,
        )
    ]


def check_pairing_index(record: ConnectionRecord, since: int, check_id: str) -> list[Finding]:
    """
    `check_id`: each pairing activation is answered with its own pairing_index.

    The index counts the pairing activations since the last handshake. A lower one is a
    leftover the server discards; it fails only when no answer carries the right one.
    """
    activations = _pairing_activations(record, since)
    judged: list[RecordedFrame] = []
    for activation, end in zip(activations, [*(a.id for a in activations[1:]), None], strict=True):
        answers = [
            f
            for f in record.frames
            if f.id > activation.id
            and (end is None or f.id < end)
            and f.inbound
            and f.type in _PAIRING_STARTS
        ]
        if not answers:
            continue
        expected = pairing_activations_since_handshake(record, activation)
        wrong = next(
            (f for f in answers if not _matches(f, expected) and not _below(f, expected)), None
        )
        if wrong is None and not any(_matches(f, expected) for f in answers):
            wrong = answers[0]
        if wrong is not None:
            return [
                _finding(
                    check_id,
                    Verdict.FAIL,
                    f"{wrong.type} carries pairing_index "
                    f"{(wrong.payload or {}).get('pairing_index')!r}, not {expected}",
                    activation,
                    wrong,
                )
            ]
        judged.append(answers[0])
    if not judged:
        return [_unanswered(record, check_id, "client/pair-init or client/pair-pending")]
    return [
        _finding(
            check_id,
            Verdict.PASS,
            "every pairing activation was answered with its own pairing_index",
            *judged,
        )
    ]


def check_cpace_lengths(record: ConnectionRecord, since: int, check_id: str) -> list[Finding]:
    """`check_id`: every fixed-size field the client sent has its base64url length."""
    sent = [
        (frame, field, length)
        for frame in record.frames
        if frame.id > since and frame.inbound
        for (message_type, field), length in _CLIENT_FIELD_LENGTHS.items()
        if frame.type == message_type and field in (frame.payload or {})
    ]
    if not any(frame.type == "client/pair-auth" for frame, _, _ in sent):
        return [_unanswered(record, check_id, "client/pair-auth")]
    for frame, field, length in sent:
        actual = redacted_length((frame.payload or {})[field])
        if actual != length:
            what = f"{actual} characters" if actual is not None else "no string"
            return [
                _finding(
                    check_id,
                    Verdict.FAIL,
                    f"{frame.type} {field} is {what}, not {length}",
                    frame,
                )
            ]
    if any(
        is_in(f, "client/pair-auth") and "pake_msg_2" not in (f.payload or {})
        for f in record.frames
        if f.id > since
    ):
        return [_finding(check_id, Verdict.FAIL, "a client/pair-auth carries no pake_msg_2")]
    fields = sorted({field for _, field, _ in sent})
    return [
        _finding(
            check_id,
            Verdict.PASS,
            f"{', '.join(fields)} had their fixed lengths",
            *{frame.id: frame for frame, _, _ in sent}.values(),
        )
    ]


def check_wrong_code(record: ConnectionRecord, since: int, prefix: str) -> list[Finding]:
    """
    `<prefix>-004`: a round run on a wrong code ends in client/pair-retry or pair/abort.

    The round is the first after frame `since`; the client sees server_kc fail to verify
    and either asks for another round with an empty payload or aborts with
    `pairing_code_mismatch`, which it may do short of the round limit.
    """
    check_id = f"{prefix}-004"
    confirm = first_after(record, since, lambda f: is_out(f, "server/pair-confirm"))
    if confirm is None:
        return [_unanswered(record, check_id, "client/pair-auth")]
    answer = first_inbound(
        record, confirm, {"client/pair-retry", "client/pair-confirm", *_REFUSALS}
    )
    if answer is None:
        if record.rejected:
            return [_not_reached(check_id)]
        ending = "closed the connection" if record.closed else "sent nothing"
        return [
            _finding(
                check_id,
                Verdict.FAIL,
                f"after server_kc from a wrong code the client {ending}, with no "
                "client/pair-retry or pair/abort 'pairing_code_mismatch'",
                confirm,
            )
        ]
    reason = (answer.payload or {}).get("reason")
    if answer.type == "client/pair-retry" and answer.payload == {}:
        verdict, message = Verdict.PASS, "the client asked for another round with client/pair-retry"
    elif answer.type == "client/pair-retry":
        verdict, message = Verdict.FAIL, "client/pair-retry carries fields; its payload is {}"
    elif answer.type == "pair/abort" and reason == "pairing_code_mismatch":
        verdict, message = Verdict.PASS, "the client aborted with 'pairing_code_mismatch'"
    elif answer.type == "client/pair-confirm":
        verdict, message = (
            Verdict.FAIL,
            "the client confirmed after a wrong code: it did not verify server_kc",
        )
    else:
        verdict, message = (
            Verdict.FAIL,
            f"the client answered a wrong code with {describe(answer)}, not client/pair-retry "
            "or pair/abort 'pairing_code_mismatch'",
        )
    return [_finding(check_id, verdict, message, confirm, answer)]


def check_verification(
    record: ConnectionRecord, since: int, check_ids: dict[Stage, str]
) -> list[Finding]:
    """
    One finding per stage in `check_ids`: the server's checks of the last confirm.

    They run in pairing.md:173-179 order, so a stage after a failed one is not
    reached. The static flow has no commit and no binding, so it judges only
    CLIENT_KC and UNWRAP. Passing UNWRAP also needs the re-handshake onto the
    delivered PSK to have keyed the session.
    """
    confirm = _last(record, since, "client/pair-confirm")
    if confirm is None:
        why = "not reached: the client sent no client/pair-confirm"
        return [_n_a(check_id, why) for check_id in check_ids.values()]
    abort = first_after(
        record,
        confirm.id,
        lambda f: (
            is_out(f, "pair/abort") and (f.payload or {}).get("reason") == "pairing_code_mismatch"
        ),
    )
    failure = next(
        (
            e
            for e in record.events
            if e.kind is EventKind.PAIRING_FAILED and (e.after_frame or 0) >= confirm.id
        ),
        None,
    )
    acknowledged = first_after(record, confirm.id, lambda f: is_out(f, "server/pair-finalize"))
    if abort is not None:
        failed: Stage | None = Stage.CLIENT_KC
        message = "client_kc did not verify: the validator aborted with 'pairing_code_mismatch'"
        frames = (confirm, abort)
    elif acknowledged is not None:
        failed = _promotion_failure(record, acknowledged)
        message = "the client did not re-handshake to the long-term PSK it delivered"
        frames = (acknowledged,)
    elif failure is not None and (stage := failure_stage(failure.detail)) is not None:
        failed, message, frames = stage, f"aiosendspin: {failure.detail}", (confirm,)
    else:
        detail = failure.detail if failure is not None else "the attempt ended unobserved"
        message = f"could not tell which check failed after client/pair-confirm: {detail}"
        return [
            _finding(check_id, Verdict.TOOL_ERROR, message, confirm)
            for check_id in check_ids.values()
        ]
    findings = []
    for stage, check_id in sorted(check_ids.items()):
        if failed is None or stage < failed:
            findings.append(_finding(check_id, Verdict.PASS, _STAGE_PASSED[stage], confirm))
        elif stage == failed:
            findings.append(_finding(check_id, Verdict.FAIL, message, *frames))
        else:
            findings.append(_n_a(check_id, "not reached: an earlier verification failed"))
    return findings


def check_qr_token(text: str) -> list[Finding]:
    """
    PAIR-DYN-QR-009: the text scanned from the client's QR is a version-1 pairing token.

    Decoding is lenient with what an operator types, but the client renders the token
    itself, so it must be the exact string pairing.md:246-260 builds: `SP:`, version
    `1` and the 39-character body, from the QR alphanumeric set only.
    """
    try:
        code = decode_pairing_code_token(text)
    except ValueError as err:
        return [
            _finding("PAIR-DYN-QR-009", Verdict.FAIL, f"the QR text is not a pairing token: {err}")
        ]
    if text.strip() != encode_pairing_code_token(code):
        return [
            _finding(
                "PAIR-DYN-QR-009",
                Verdict.FAIL,
                "the QR text decodes, but is not the exact token the spec builds: it differs "
                "in its prefix, case, transliteration or length",
            )
        ]
    return [_finding("PAIR-DYN-QR-009", Verdict.PASS, "the QR text is a version-1 pairing token")]


def check_static_pending(record: ConnectionRecord, since: int) -> list[Finding]:
    """PAIR-STATIC-001: with no pairing window open, the client answers with client/pair-pending."""
    activation = next(iter(_pairing_activations(record, since)), None)
    if activation is None:
        return [_n_a("PAIR-STATIC-001", "the validator sent no pairing activation")]
    answer = first_inbound(record, activation, {*_PAIRING_STARTS, *_REFUSALS})
    if answer is not None and answer.type == "client/pair-pending":
        return [
            _finding(
                "PAIR-STATIC-001",
                Verdict.PASS,
                "the client held the attempt back with client/pair-pending until its window opened",
                activation,
                answer,
            )
        ]
    if answer is None:
        return [_unanswered(record, "PAIR-STATIC-001", "client/pair-pending")]
    message = (
        "the client started the attempt with no pairing window open; if one was open, "
        "run the step again with it closed"
        if answer.type == "client/pair-init"
        else f"the client answered with {describe(answer)}, not client/pair-pending"
    )
    return [_finding("PAIR-STATIC-001", Verdict.FAIL, message, activation, answer)]


def check_static_shape(record: ConnectionRecord, since: int) -> list[Finding]:
    """PAIR-STATIC-003: no commit_B, no wrapped_nonce_B and no client/pair-retry."""
    pair_init = first_after(record, since, lambda f: is_in(f, "client/pair-init"))
    if pair_init is None:
        return [_unanswered(record, "PAIR-STATIC-003", "client/pair-init")]
    misplaced = first_after(
        record,
        since,
        lambda f: (
            is_in(f, "client/pair-retry")
            or (is_in(f, "client/pair-init") and "commit_B" in (f.payload or {}))
            or (is_in(f, "client/pair-confirm") and "wrapped_nonce_B" in (f.payload or {}))
        ),
    )
    if misplaced is not None:
        return [
            _finding(
                "PAIR-STATIC-003",
                Verdict.FAIL,
                f"{misplaced.type} belongs to the dynamic flow, or carries a field that does",
                misplaced,
            )
        ]
    return [
        _finding(
            "PAIR-STATIC-003",
            Verdict.PASS,
            "the client sent no commit_B, wrapped_nonce_B or client/pair-retry",
            pair_init,
        )
    ]


def check_static_order(record: ConnectionRecord, since: int) -> list[Finding]:
    """PAIR-STATIC-004: the client starts the attempt with client/pair-init, unprompted."""
    pair_init = first_after(record, since, lambda f: is_in(f, "client/pair-init"))
    auth = first_after(record, since, lambda f: is_out(f, "server/pair-auth"))
    if auth is not None and (pair_init is None or auth.id < pair_init.id):
        return [
            _finding(
                "PAIR-STATIC-004",
                Verdict.TOOL_ERROR,
                "the validator sent server/pair-auth before client/pair-init",
                auth,
            )
        ]
    if pair_init is None:
        return [_unanswered(record, "PAIR-STATIC-004", "client/pair-init")]
    return [
        _finding(
            "PAIR-STATIC-004",
            Verdict.PASS,
            "the client sent client/pair-init before any server/pair-auth",
            pair_init,
            *(auth,) if auth is not None else (),
        )
    ]


def check_lockout(record: ConnectionRecord, since: int) -> list[Finding]:
    """
    PAIR-STATIC-009 and -010: five failed attempts close the window; a sixth is held back.

    Each of the first five pairing activations after `since` ran on a wrong code, which
    the client must end with pair/abort `pairing_code_mismatch`. The sixth must be
    answered with client/pair-pending.
    """
    activations = _pairing_activations(record, since)
    ends = [*(a.id for a in activations[1:]), None]
    for number, (activation, end) in enumerate(
        zip(activations[:LOCKOUT_ATTEMPTS], ends, strict=False), start=1
    ):
        abort = first_after(
            record,
            activation.id,
            lambda f, end=end: (end is None or f.id < end) and is_in(f, "pair/abort"),
        )
        if abort is None or (abort.payload or {}).get("reason") != "pairing_code_mismatch":
            what = describe(abort) if abort is not None else "no pair/abort"
            return [
                _finding(
                    "PAIR-STATIC-009",
                    Verdict.FAIL,
                    f"wrong-code attempt {number} ended with {what}, not pair/abort "
                    "'pairing_code_mismatch'",
                    activation,
                    *(abort,) if abort is not None else (),
                ),
                _n_a("PAIR-STATIC-010", "not reached: an earlier attempt did not fail cleanly"),
            ]
    if len(activations) < LOCKOUT_ATTEMPTS:
        return [
            _not_reached("PAIR-STATIC-009"),
            _not_reached("PAIR-STATIC-010"),
        ]
    failed = _finding(
        "PAIR-STATIC-009",
        Verdict.PASS,
        f"the client aborted each of {LOCKOUT_ATTEMPTS} wrong-code attempts with "
        "'pairing_code_mismatch'",
        *activations[:LOCKOUT_ATTEMPTS],
    )
    if len(activations) <= LOCKOUT_ATTEMPTS:
        return [failed, _not_reached("PAIR-STATIC-010")]
    sixth = activations[LOCKOUT_ATTEMPTS]
    answer = first_inbound(record, sixth, {*_PAIRING_STARTS, *_REFUSALS})
    if answer is not None and answer.type == "client/pair-pending":
        return [
            failed,
            _finding(
                "PAIR-STATIC-010",
                Verdict.PASS,
                "the window closed: the sixth attempt was held back with client/pair-pending",
                sixth,
                answer,
            ),
        ]
    what = describe(answer) if answer is not None else "nothing"
    return [
        failed,
        _finding(
            "PAIR-STATIC-010",
            Verdict.FAIL,
            f"after {LOCKOUT_ATTEMPTS} failed attempts the client answered a sixth with {what}, "
            "not client/pair-pending: the window stayed open",
            sixth,
            *(answer,) if answer is not None else (),
        ),
    ]


def check_cancel(record: ConnectionRecord, since: int) -> list[Finding]:
    """PAIR-CTL-001: after pair/abort `user_cancelled` and a fresh activation, the client stays."""
    abort = first_after(
        record,
        since,
        lambda f: is_out(f, "pair/abort") and (f.payload or {}).get("reason") == "user_cancelled",
    )
    if abort is None:
        return [
            _finding(
                "PAIR-CTL-001",
                Verdict.TOOL_ERROR,
                "the validator did not cancel the attempt with pair/abort 'user_cancelled'",
            )
        ]
    leave = first_after(
        record,
        abort.id,
        lambda f: is_out(f, "server/activate") and "pairing" not in activities(f.payload or {}),
    )
    goodbye = first_inbound(record, abort, {"client/goodbye"})
    if goodbye is not None or record.closed or record.rejected:
        what = describe(goodbye) if goodbye is not None else "the connection ended"
        return [
            _finding(
                "PAIR-CTL-001",
                Verdict.FAIL,
                f"the client did not drop the cancelled attempt cleanly: {what}",
                abort,
                *(goodbye,) if goodbye is not None else (),
            )
        ]
    if leave is None:
        return [
            _finding(
                "PAIR-CTL-001",
                Verdict.TOOL_ERROR,
                "the validator sent no server/activate after cancelling",
                abort,
            )
        ]
    return [
        _finding(
            "PAIR-CTL-001",
            Verdict.PASS,
            "the client dropped the cancelled attempt and kept the connection",
            abort,
            leave,
        )
    ]


def check_timeout(record: ConnectionRecord, since: int) -> list[Finding]:
    """PAIR-CTL-002: an attempt left alone ends with the client's pair/abort `attempt_timeout`."""
    pair_init = first_after(record, since, lambda f: is_in(f, "client/pair-init"))
    if pair_init is None:
        return [_unanswered(record, "PAIR-CTL-002", "client/pair-init")]
    abort = first_after(record, pair_init.id, lambda f: is_in(f, "pair/abort"))
    if abort is not None and (abort.payload or {}).get("reason") == "attempt_timeout":
        return [
            _finding(
                "PAIR-CTL-002",
                Verdict.PASS,
                "the client ended the idle attempt with pair/abort 'attempt_timeout'",
                pair_init,
                abort,
            )
        ]
    if abort is None and not record.closed and _server_gave_up(record, pair_init):
        return [
            _finding(
                "PAIR-CTL-002",
                Verdict.WARN,
                "the client had not ended the idle attempt when the validator gave up, after "
                "3 minutes; the spec recommends a 2-minute attempt timeout",
                pair_init,
            )
        ]
    what = (
        describe(abort)
        if abort is not None
        else "the connection ended"
        if record.closed
        else "no pair/abort"
    )
    return [
        _finding(
            "PAIR-CTL-002",
            Verdict.FAIL,
            f"the idle attempt ended with {what}, not pair/abort 'attempt_timeout'",
            pair_init,
            *(abort,) if abort is not None else (),
        )
    ]


def check_presentation(check_id: str, typed: str, digits: int) -> list[Finding]:
    """
    `check_id`: the code the user typed as shown is grouped `123-456` or `1234-5678`.

    The message names only the shape typed, never a digit of the code.
    """
    half = digits // 2
    if re.fullmatch(rf"\d{{{half}}}-\d{{{half}}}", typed.strip()):
        return [human(check_id, Verdict.PASS, "the code was shown grouped with a hyphen")]
    shape = re.sub(r"\d", "#", typed.strip())
    expected = f"{'#' * half}-{'#' * half}"
    return [
        human(
            check_id,
            Verdict.WARN,
            f"the code was shown as {shape!r}, not grouped as {expected!r}",
        )
    ]


def human(check_id: str, verdict: Verdict, message: str) -> Finding:
    """Return a finding resting on an answer the user gave."""
    return Finding(check_id, verdict, SPEC_REFS[check_id], message, evidence=Evidence.HUMAN)


def failure_stage(detail: str | None) -> Stage | None:
    """Return the verification stage aiosendspin's failure `detail` names, or None."""
    if detail is None:
        return None
    return next((stage for text, stage in FAILURE_STAGES.items() if text in detail), None)


def not_run(check_ids: Sequence[str], verdict: Verdict, message: str) -> list[Finding]:
    """Return one finding per check of a step that did not run: skipped, or not reached."""
    return [_finding(check_id, verdict, message) for check_id in check_ids]


def not_applicable(check_ids: Sequence[str], message: str) -> list[Finding]:
    """Return an N/A finding for each of `check_ids`."""
    return not_run(check_ids, Verdict.NOT_APPLICABLE, message)


def _promotion_failure(record: ConnectionRecord, acknowledged: RecordedFrame) -> Stage | None:
    """Return UNWRAP unless the session was keyed by a long-term PSK after `acknowledged`."""
    keyed = next(
        (
            e
            for e in record.events
            if e.kind is EventKind.KEYED and (e.after_frame or 0) > acknowledged.id
        ),
        None,
    )
    return None if keyed is not None and keyed.psk_category == "long_term" else Stage.UNWRAP


def _server_gave_up(record: ConnectionRecord, after: RecordedFrame) -> bool:
    """Whether aiosendspin's own bound on the attempt expired after frame `after`."""
    return any(
        e.kind is EventKind.PAIRING_FAILED
        and (e.after_frame or 0) >= after.id
        and (e.detail or "").startswith("PairingTimeoutError")
        for e in record.events
    )


def _pairing_activations(record: ConnectionRecord, since: int) -> list[RecordedFrame]:
    return [
        f
        for f in record.frames
        if f.id > since
        and is_out(f, "server/activate")
        and "pairing" in activities(f.payload or {})
    ]


def _last(record: ConnectionRecord, since: int, message_type: str) -> RecordedFrame | None:
    return next(
        (f for f in reversed(record.frames) if f.id > since and is_in(f, message_type)), None
    )


def _matches(frame: RecordedFrame, expected: int) -> bool:
    index = (frame.payload or {}).get("pairing_index")
    return index == expected and not isinstance(index, bool)


def _below(frame: RecordedFrame, expected: int) -> bool:
    index = (frame.payload or {}).get("pairing_index")
    return isinstance(index, int) and not isinstance(index, bool) and index < expected


def _unanswered(record: ConnectionRecord, check_id: str, what: str) -> Finding:
    """Return the finding for a step whose client never sent `what`."""
    if record.rejected:
        return _not_reached(check_id)
    return _finding(check_id, Verdict.FAIL, f"the client sent no {what}")


def _finding(check_id: str, verdict: Verdict, message: str, *frames: RecordedFrame) -> Finding:
    return Finding(
        check_id, verdict, SPEC_REFS[check_id], message, frames=tuple(f.id for f in frames)
    )


def _n_a(check_id: str, message: str) -> Finding:
    return _finding(check_id, Verdict.NOT_APPLICABLE, message)


def _not_reached(check_id: str) -> Finding:
    return _n_a(check_id, "not reached: the attempt ended at an earlier failure")
