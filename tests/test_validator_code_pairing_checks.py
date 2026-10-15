"""The pairing-code checks judge hand-built connections correctly."""

import inspect
import re
import unittest
from typing import Any

from conformance._vendor.aiosendspin.noise import pairing as noise_pairing
from conformance._vendor.aiosendspin.noise.pairing_token import encode_pairing_code_token
from conformance.validator import code_pairing_checks as checks
from conformance.validator.checks import ConnectionRecord
from conformance.validator.code_pairing_checks import Stage
from conformance.validator.findings import Evidence, Finding, Verdict
from conformance.validator.recorder import EventKind, RecordedEvent, RecordedFrame

# The QR-format pairing code reference vector from pairing.md:165-169.
VECTOR_QR_CODE = bytes(range(0xE0, 0xF8))
VECTOR_QR_TOKEN = "SP:14DQ6FY7E4XTOP9HJ5LV6Z3PO57YPD4XT6T97N5Y"
DYNAMIC_ACTIVATION = {
    "activities": ["pairing"],
    "active_roles": [],
    "pairing": {"method": "dynamic_pairing_code", "format": "digits"},
}
STATIC_ACTIVATION = {
    "activities": ["pairing"],
    "active_roles": [],
    "pairing": {"method": "static_pairing_code"},
}
COMMIT = "c" * 43
DYNAMIC_STAGES = {stage: f"PAIR-DYN-00{stage + 5}" for stage in Stage}
STATIC_STAGES = {Stage.CLIENT_KC: "PAIR-STATIC-006", Stage.UNWRAP: "PAIR-STATIC-007"}


class Connection:
    """Builds one connection's record frame by frame, as the recorder would."""

    def __init__(self) -> None:
        self.frames: list[RecordedFrame] = []
        self.events: list[RecordedEvent] = []

    def out(self, message_type: str, payload: dict[str, Any] | None = None) -> int:
        return self._add("out", message_type, payload)

    def came(self, message_type: str, payload: dict[str, Any] | None = None) -> int:
        return self._add("in", message_type, payload)

    def event(self, kind: EventKind, **kw: Any) -> None:
        self.events.append(RecordedEvent(1, "client", kind, self.last, **kw))

    def round(self, *, retry: bool = False) -> None:
        """Add one CPace round, ending in the client's retry when `retry`."""
        self.out("server/pair-auth", {"pake_msg_1": "<redacted:43>"})
        self.came("client/pair-auth", {"pake_msg_2": "<redacted:43>"})
        self.out("server/pair-confirm", {"server_kc": "<redacted:86>"})
        if retry:
            self.came("client/pair-retry")

    def dynamic(self, *, wrong_first: bool = True, finalize: bool = True) -> "Connection":
        """Add a dynamic-code attempt: a wrong round when `wrong_first`, then a right one."""
        self.out("server/activate", DYNAMIC_ACTIVATION)
        self.came("client/pair-init", {"pairing_index": 1, "commit_B": COMMIT})
        self.out("server/pair-init", {"nonce_A": "n" * 43})
        if wrong_first:
            self.round(retry=True)
            self.out("server/pair-init")
        self.round()
        self.came(
            "client/pair-confirm",
            {"client_kc": "<redacted:86>", "wrapped_nonce_B": "<redacted:64>"},
        )
        self.came("client/pair-finalize", {"wrapped_psk": "<redacted:64>"})
        if finalize:
            self.promote()
        return self

    def static(self, *, pending: bool = True) -> "Connection":
        """Add a static-code attempt, held back first when `pending`."""
        self.out("server/activate", STATIC_ACTIVATION)
        if pending:
            self.came("client/pair-pending", {"pairing_index": 1})
        self.came("client/pair-init", {"pairing_index": 1})
        self.round()
        self.came("client/pair-confirm", {"client_kc": "<redacted:86>"})
        self.came("client/pair-finalize", {"wrapped_psk": "<redacted:64>"})
        self.promote()
        return self

    def promote(self) -> None:
        self.out("server/pair-finalize")
        self.out("noise/handshake", {"data": "lt1"})
        self.came("noise/handshake", {"data": "lt2"})
        self.event(EventKind.KEYED, psk_category="long_term")
        self.out("server/activate", {"activities": ["playback"]})

    @property
    def last(self) -> int:
        return self.frames[-1].id if self.frames else 0

    def record(self) -> ConnectionRecord:
        return ConnectionRecord(1, tuple(self.frames), (), tuple(self.events))

    def _add(self, direction: str, message_type: str, payload: dict[str, Any] | None) -> int:
        frame_id = self.last + 1
        self.frames.append(
            RecordedFrame(
                id=frame_id,
                connection=1,
                client_id="client",
                direction=direction,
                phase="transport",
                monotonic_us=frame_id,
                size=0,
                message={"type": message_type, "payload": payload or {}},
            )
        )
        return frame_id


def only(findings: list[Finding]) -> Finding:
    assert len(findings) == 1, findings
    return findings[0]


def verdicts(findings: list[Finding]) -> dict[str, Verdict]:
    return {f.id: f.verdict for f in findings}


class DynamicTests(unittest.TestCase):
    def test_a_conformant_attempt_passes_every_machine_check(self) -> None:
        record = Connection().dynamic().record()
        findings = [
            *checks.check_commit(record, 0, checks.DYNAMIC),
            *checks.check_pairing_index(record, 0, "PAIR-DYN-002"),
            *checks.check_cpace_lengths(record, 0, "PAIR-DYN-003"),
            *checks.check_wrong_code(record, 0, checks.DYNAMIC),
            *checks.check_verification(record, 0, DYNAMIC_STAGES),
        ]
        self.assertEqual(set(verdicts(findings).values()), {Verdict.PASS}, findings)
        self.assertEqual(len(findings), 8)

    def test_a_pair_init_without_commit_fails(self) -> None:
        connection = Connection()
        connection.out("server/activate", DYNAMIC_ACTIVATION)
        connection.came("client/pair-init", {"pairing_index": 1, "commit_B": "short"})
        finding = only(checks.check_commit(connection.record(), 0, checks.DYNAMIC))
        self.assertIs(finding.verdict, Verdict.FAIL)
        self.assertIn("not 43 characters", finding.message)

    def test_no_pair_init_fails(self) -> None:
        connection = Connection()
        connection.out("server/activate", DYNAMIC_ACTIVATION)
        connection.came("pair/abort", {"reason": "method_not_supported"})
        finding = only(checks.check_commit(connection.record(), 0, checks.DYNAMIC_QR))
        self.assertEqual((finding.id, finding.verdict), ("PAIR-DYN-QR-001", Verdict.FAIL))

    def test_a_pairing_index_must_count_activations_since_the_handshake(self) -> None:
        connection = Connection()
        connection.out("server/activate", DYNAMIC_ACTIVATION)
        connection.came("client/pair-init", {"pairing_index": 1, "commit_B": COMMIT})
        connection.out("server/activate", DYNAMIC_ACTIVATION)
        # A leftover from the first activation is discarded; the answer to the second counts.
        connection.came("client/pair-pending", {"pairing_index": 1})
        connection.came("client/pair-init", {"pairing_index": 2, "commit_B": COMMIT})
        record = connection.record()
        self.assertIs(
            only(checks.check_pairing_index(record, 0, "PAIR-DYN-002")).verdict, Verdict.PASS
        )

        connection.out("noise/handshake", {"data": "1"})
        connection.out("server/activate", DYNAMIC_ACTIVATION)
        connection.came("client/pair-init", {"pairing_index": 3, "commit_B": COMMIT})
        finding = only(checks.check_pairing_index(connection.record(), 0, "PAIR-DYN-002"))
        self.assertIs(finding.verdict, Verdict.FAIL)
        self.assertIn("pairing_index 3, not 1", finding.message)

    def test_a_field_of_the_wrong_length_fails(self) -> None:
        connection = Connection()
        connection.out("server/activate", DYNAMIC_ACTIVATION)
        connection.came("client/pair-init", {"pairing_index": 1, "commit_B": COMMIT})
        connection.out("server/pair-auth", {"pake_msg_1": "<redacted:43>"})
        connection.came("client/pair-auth", {"pake_msg_2": "<redacted:44>"})
        finding = only(checks.check_cpace_lengths(connection.record(), 0, "PAIR-DYN-003"))
        self.assertIs(finding.verdict, Verdict.FAIL)
        self.assertIn("pake_msg_2 is 44 characters, not 43", finding.message)

    def test_a_wrong_code_may_end_in_an_abort(self) -> None:
        connection = Connection()
        connection.out("server/activate", DYNAMIC_ACTIVATION)
        connection.came("client/pair-init", {"pairing_index": 1, "commit_B": COMMIT})
        connection.out("server/pair-init", {"nonce_A": "n" * 43})
        connection.round()
        connection.came("pair/abort", {"reason": "pairing_code_mismatch"})
        self.assertIs(
            only(checks.check_wrong_code(connection.record(), 0, checks.DYNAMIC)).verdict,
            Verdict.PASS,
        )

    def test_a_wrong_code_answered_otherwise_fails(self) -> None:
        cases = {
            "confirm": ("client/pair-confirm", {"client_kc": "<redacted:86>"}),
            "retry with fields": ("client/pair-retry", {"round": 2}),
            "other abort": ("pair/abort", {"reason": "user_cancelled"}),
            "silence": None,
        }
        for name, answer in cases.items():
            with self.subTest(name):
                connection = Connection()
                connection.out("server/activate", DYNAMIC_ACTIVATION)
                connection.came("client/pair-init", {"pairing_index": 1, "commit_B": COMMIT})
                connection.out("server/pair-init", {"nonce_A": "n" * 43})
                connection.round()
                if answer is not None:
                    connection.came(*answer)
                finding = only(checks.check_wrong_code(connection.record(), 0, checks.DYNAMIC))
                self.assertIs(finding.verdict, Verdict.FAIL)

    def test_a_failed_client_kc_leaves_later_checks_unreached(self) -> None:
        connection = Connection().dynamic(finalize=False)
        connection.out("pair/abort", {"reason": "pairing_code_mismatch"})
        findings = checks.check_verification(connection.record(), 0, DYNAMIC_STAGES)
        self.assertEqual(
            [f.verdict for f in findings],
            [Verdict.FAIL, Verdict.NOT_APPLICABLE, Verdict.NOT_APPLICABLE, Verdict.NOT_APPLICABLE],
        )

    def test_each_failure_aiosendspin_reports_fails_its_own_stage(self) -> None:
        cases = {
            "revealed nonce_B does not match commit_B": Stage.COMMIT,
            "entered pairing code is not bound to this connection": Stage.BINDING,
            "malformed wrapped_psk: AEAD failure": Stage.UNWRAP,
        }
        for reason, failed in cases.items():
            with self.subTest(reason):
                connection = Connection().dynamic(finalize=False)
                connection.event(EventKind.PAIRING_FAILED, detail=f"PairingError: {reason}")
                findings = checks.check_verification(connection.record(), 0, DYNAMIC_STAGES)
                expected = [
                    Verdict.PASS
                    if stage < failed
                    else Verdict.FAIL
                    if stage == failed
                    else Verdict.NOT_APPLICABLE
                    for stage in Stage
                ]
                self.assertEqual([f.verdict for f in findings], expected)
                self.assertIn(reason, findings[failed].message)

    def test_an_unmapped_failure_is_a_tool_error_never_a_pass(self) -> None:
        connection = Connection().dynamic(finalize=False)
        connection.event(EventKind.PAIRING_FAILED, detail="PairingError: something new")
        findings = checks.check_verification(connection.record(), 0, DYNAMIC_STAGES)
        self.assertEqual({f.verdict for f in findings}, {Verdict.TOOL_ERROR})

    def test_finalizing_without_promotion_fails_the_unwrap_stage(self) -> None:
        connection = Connection().dynamic(finalize=False)
        connection.out("server/pair-finalize")
        findings = checks.check_verification(connection.record(), 0, DYNAMIC_STAGES)
        self.assertEqual(findings[Stage.UNWRAP].verdict, Verdict.FAIL)
        self.assertEqual(findings[Stage.BINDING].verdict, Verdict.PASS)

    def test_no_confirm_leaves_verification_unreached(self) -> None:
        connection = Connection()
        connection.out("server/activate", DYNAMIC_ACTIVATION)
        findings = checks.check_verification(connection.record(), 0, DYNAMIC_STAGES)
        self.assertEqual({f.verdict for f in findings}, {Verdict.NOT_APPLICABLE})


class FailureStageTests(unittest.TestCase):
    """Every reason the bundled aiosendspin can fail a confirmed attempt for is mapped."""

    # Reasons raised before client/pair-confirm arrives, which the stages never see.
    BEFORE_CONFIRM = frozenset(
        {
            "client/pair-init missing commit_B for dynamic pairing code",
            "client/pair-init carries commit_B for static pairing code",
            "static pairing code must be exactly 8 decimal digits",
            "malformed commit_B",
        }
    )

    def test_every_vendored_reason_has_a_stage(self) -> None:
        source = "".join(
            inspect.getsource(function)
            for function in (
                noise_pairing.run_dynamic_pairing_code_server,
                noise_pairing.run_static_pairing_code_server,
                noise_pairing._commit_finalize,  # noqa: SLF001
                noise_pairing._unwrap_psk,  # noqa: SLF001
                noise_pairing.abort_pairing,
            )
        )
        reasons = set(re.findall(r'Error\(\s*"([^"]+)"', source))
        reasons |= {f"malformed {field}" for field in re.findall(r'"(\w+)",\s*expect_len', source)}
        self.assertTrue(reasons)
        for reason in reasons - self.BEFORE_CONFIRM:
            with self.subTest(reason):
                self.assertIsNotNone(checks.failure_stage(f"PairingError: {reason}"), reason)

    def test_aborts_and_timeouts_have_a_stage(self) -> None:
        for detail in (
            "LocalPairingAbortError: pairing aborted: pairing_code_mismatch",
            "PairingTimeoutError: the rest of the attempt did not arrive in time",
            "PairingError: connection closed while awaiting ClientPairFinalizeMessage",
        ):
            with self.subTest(detail):
                self.assertIsNotNone(checks.failure_stage(detail))


class QrTokenTests(unittest.TestCase):
    def test_the_reference_vector_decodes(self) -> None:
        self.assertEqual(encode_pairing_code_token(VECTOR_QR_CODE), VECTOR_QR_TOKEN)
        finding = only(checks.check_qr_token(f" {VECTOR_QR_TOKEN}\n"))
        self.assertEqual((finding.id, finding.verdict), ("PAIR-DYN-QR-009", Verdict.PASS))

    def test_a_token_only_lenient_decoding_accepts_fails(self) -> None:
        for name, text in {
            "lower case": VECTOR_QR_TOKEN.lower(),
            "no prefix": VECTOR_QR_TOKEN.removeprefix("SP:"),
            "untransliterated": VECTOR_QR_TOKEN.replace("9", "2"),
            "extra payload": encode_pairing_code_token(VECTOR_QR_CODE)[:-1] + "AAAAAAA",
        }.items():
            with self.subTest(name):
                finding = only(checks.check_qr_token(text))
                self.assertIs(finding.verdict, Verdict.FAIL)
                self.assertNotIn(VECTOR_QR_TOKEN[4:], finding.message)

    def test_a_pairing_psk_token_is_not_a_qr_code(self) -> None:
        finding = only(checks.check_qr_token("SP:0AAAQEAYEAUDAOCAJBIFQYDIOB4IBCEQTCQKRMFYYDENBWHA"))
        self.assertIs(finding.verdict, Verdict.FAIL)


class StaticTests(unittest.TestCase):
    def test_a_conformant_attempt_passes_every_machine_check(self) -> None:
        record = Connection().static().record()
        findings = [
            *checks.check_static_pending(record, 0),
            *checks.check_pairing_index(record, 0, "PAIR-STATIC-002"),
            *checks.check_static_shape(record, 0),
            *checks.check_static_order(record, 0),
            *checks.check_cpace_lengths(record, 0, "PAIR-STATIC-005"),
            *checks.check_verification(record, 0, STATIC_STAGES),
        ]
        self.assertEqual(set(verdicts(findings).values()), {Verdict.PASS}, findings)
        self.assertEqual(len(findings), 7)

    def test_starting_with_the_window_closed_fails(self) -> None:
        finding = only(checks.check_static_pending(Connection().static(pending=False).record(), 0))
        self.assertIs(finding.verdict, Verdict.FAIL)
        self.assertIn("no pairing window open", finding.message)

    def test_dynamic_flow_messages_fail(self) -> None:
        connection = Connection()
        connection.out("server/activate", STATIC_ACTIVATION)
        connection.came("client/pair-init", {"pairing_index": 1})
        connection.round(retry=True)
        finding = only(checks.check_static_shape(connection.record(), 0))
        self.assertIs(finding.verdict, Verdict.FAIL)
        self.assertEqual(finding.frames, (connection.last,))

    def test_pair_auth_before_pair_init_is_the_validators_fault(self) -> None:
        connection = Connection()
        connection.out("server/activate", STATIC_ACTIVATION)
        connection.out("server/pair-auth", {"pake_msg_1": "<redacted:43>"})
        connection.came("client/pair-init", {"pairing_index": 1})
        self.assertIs(
            only(checks.check_static_order(connection.record(), 0)).verdict, Verdict.TOOL_ERROR
        )


class LockoutTests(unittest.TestCase):
    def lockout(self, *, aborts: int, sixth: str) -> list[Finding]:
        connection = Connection()
        for attempt in range(checks.LOCKOUT_ATTEMPTS):
            connection.out("server/activate", STATIC_ACTIVATION)
            connection.came("client/pair-init", {"pairing_index": 1})
            connection.round()
            reason = "pairing_code_mismatch" if attempt < aborts else "user_cancelled"
            connection.came("pair/abort", {"reason": reason})
            connection.out("server/activate", {"activities": []})
        connection.out("server/activate", STATIC_ACTIVATION)
        connection.came(sixth, {"pairing_index": 1})
        return checks.check_lockout(connection.record(), 0)

    def test_a_window_closed_by_five_failures_holds_the_sixth_back(self) -> None:
        findings = self.lockout(aborts=5, sixth="client/pair-pending")
        self.assertEqual([f.verdict for f in findings], [Verdict.PASS, Verdict.PASS])

    def test_a_window_still_open_after_five_failures_fails(self) -> None:
        findings = self.lockout(aborts=5, sixth="client/pair-init")
        self.assertEqual([f.verdict for f in findings], [Verdict.PASS, Verdict.FAIL])

    def test_an_attempt_not_failed_with_mismatch_fails(self) -> None:
        findings = self.lockout(aborts=3, sixth="client/pair-pending")
        self.assertEqual([f.verdict for f in findings], [Verdict.FAIL, Verdict.NOT_APPLICABLE])
        self.assertIn("attempt 4", findings[0].message)


class ControlTests(unittest.TestCase):
    def cancelled(self) -> Connection:
        connection = Connection()
        connection.out("server/activate", DYNAMIC_ACTIVATION)
        connection.came("client/pair-init", {"pairing_index": 1, "commit_B": COMMIT})
        connection.out("server/pair-init", {"nonce_A": "n" * 43})
        connection.out("pair/abort", {"reason": "user_cancelled"})
        connection.out("server/activate", {"activities": ["playback"]})
        return connection

    def test_a_client_that_stays_after_a_cancel_passes(self) -> None:
        self.assertIs(only(checks.check_cancel(self.cancelled().record(), 0)).verdict, Verdict.PASS)

    def test_a_client_that_leaves_after_a_cancel_fails(self) -> None:
        connection = self.cancelled()
        connection.came("client/goodbye", {"reason": "shutdown"})
        finding = only(checks.check_cancel(connection.record(), 0))
        self.assertIs(finding.verdict, Verdict.FAIL)
        self.assertIn("'shutdown'", finding.message)

    def test_the_attempt_timeout_must_be_named(self) -> None:
        for reason, verdict in (
            ("attempt_timeout", Verdict.PASS),
            ("user_cancelled", Verdict.FAIL),
        ):
            with self.subTest(reason):
                connection = Connection()
                connection.out("server/activate", DYNAMIC_ACTIVATION)
                connection.came("client/pair-init", {"pairing_index": 1, "commit_B": COMMIT})
                connection.came("pair/abort", {"reason": reason})
                self.assertIs(only(checks.check_timeout(connection.record(), 0)).verdict, verdict)

    def test_no_abort_before_the_validator_gives_up_warns(self) -> None:
        connection = Connection()
        connection.out("server/activate", DYNAMIC_ACTIVATION)
        connection.came("client/pair-init", {"pairing_index": 1, "commit_B": COMMIT})
        connection.event(
            EventKind.PAIRING_FAILED,
            detail="PairingTimeoutError: the rest of the attempt did not arrive in time",
        )
        finding = only(checks.check_timeout(connection.record(), 0))
        self.assertIs(finding.verdict, Verdict.WARN)

    def test_closing_instead_of_aborting_fails(self) -> None:
        connection = Connection()
        connection.out("server/activate", DYNAMIC_ACTIVATION)
        connection.came("client/pair-init", {"pairing_index": 1, "commit_B": COMMIT})
        connection.event(EventKind.CLOSED)
        finding = only(checks.check_timeout(connection.record(), 0))
        self.assertIs(finding.verdict, Verdict.FAIL)


class PresentationTests(unittest.TestCase):
    def test_grouped_codes_pass_and_others_warn_without_naming_digits(self) -> None:
        self.assertIs(
            only(checks.check_presentation("PAIR-DYN-009", "123-456", 6)).verdict, Verdict.PASS
        )
        self.assertIs(
            only(checks.check_presentation("PAIR-STATIC-008", "1234-5678", 8)).verdict,
            Verdict.PASS,
        )
        finding = only(checks.check_presentation("PAIR-DYN-009", "123456", 6))
        self.assertEqual((finding.verdict, finding.evidence), (Verdict.WARN, Evidence.HUMAN))
        self.assertIn("'######'", finding.message)
        self.assertNotIn("123", finding.message)


if __name__ == "__main__":
    unittest.main()
