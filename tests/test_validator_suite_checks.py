"""The unpaired, pairing and post-pairing checks judge hand-built connections correctly."""

import unittest
from typing import Any

from conformance._vendor.aiosendspin.noise import b64url_encode
from conformance.validator import suite_checks
from conformance.validator.checks import ConnectionRecord
from conformance.validator.findings import Finding, Verdict
from conformance.validator.recorder import (
    EventKind,
    RecordedEvent,
    RecordedFrame,
    RecordedNoncompliance,
)

# The pairing token reference vector from pairing.md:97-101.
VECTOR_CLIENT_ID = b64url_encode(bytes(range(32)))
VECTOR_TOKEN = (
    "SP:0AAAQEAYEAUDAOCAJBIFQYDIOB4IBCEQTCQKRMFYYDENBWHA5DYP6BYPC4PSOLZXH5DU6V97M5XXO74HR6LZ"
    "7J5PW674PT6X37T6757Y"
)
CLIENT_ID = "IjlDhKVumKnfsZCkQ389bq2a6oHQ_y_xm84lYXJ3yCo"
ROLES = ["player@v1", "controller@v1"]
PAIRING_ACTIVATION = {
    "activities": ["playback", "pairing"],
    "pairing": {"method": "pairing_psk"},
}


class Connection:
    """Builds one connection's record frame by frame, as the recorder would."""

    def __init__(self) -> None:
        self.frames: list[RecordedFrame] = []
        self.events: list[RecordedEvent] = []
        self.noncompliance: list[RecordedNoncompliance] = []

    def out(self, message_type: str, payload: dict[str, Any] | None = None, **kw: Any) -> int:
        return self._add("out", message_type, payload, **kw)

    def came(self, message_type: str, payload: dict[str, Any] | None = None, **kw: Any) -> int:
        return self._add("in", message_type, payload, **kw)

    def event(self, kind: EventKind, psk_category: str | None = None) -> None:
        self.events.append(
            RecordedEvent(1, CLIENT_ID, kind, self.last, psk_category=psk_category)
        )

    def opening(
        self, *, keyed: str | None = "sentinel", unpaired: bool = True, mismatch: bool = False
    ) -> "Connection":
        """Add an opening exchange, keyed by `keyed`, or one whose handshake failed if None."""
        self.came("client/init", {"client_id": CLIENT_ID}, phase="cleartext")
        self.out("server/init", phase="cleartext")
        self.out("noise/handshake", {"data": "1"}, phase="cleartext")
        self.came("noise/handshake", {"data": "2"}, phase="cleartext")
        if keyed is None:
            return self
        self.out("server/hello")
        self.came(
            "client/hello",
            {
                "supported_roles": ROLES,
                "supported_pair_methods": {"pairing_psk": {}},
                "unpaired_access": {"enabled": unpaired},
            },
        )
        self.event(EventKind.KEYED, keyed)
        if mismatch:
            self.event(EventKind.CREDENTIAL_MISMATCH)
        return self

    def pairing(self, *, finalize: dict[str, Any] | None = None) -> "Connection":
        """Add a complete Pairing PSK exchange, ending on the paired activation."""
        self.out("noise/handshake", {"data": "pr1"})
        self.came("noise/handshake", {"data": "pr2"})
        self.out("server/activate", PAIRING_ACTIVATION)
        self.came("client/pair-init", {"pairing_index": 1})
        self.came("client/pair-finalize", finalize or {"long_term_psk": "<redacted>"})
        self.out("server/pair-finalize")
        self.out("noise/handshake", {"data": "lt1"})
        self.came("noise/handshake", {"data": "lt2"})
        self.event(EventKind.KEYED, "long_term")
        self.out("server/activate", {"activities": ["playback"]})
        return self

    @property
    def last(self) -> int:
        return self.frames[-1].id if self.frames else 0

    def record(self) -> ConnectionRecord:
        return ConnectionRecord(
            1, tuple(self.frames), tuple(self.noncompliance), tuple(self.events)
        )

    def _add(
        self,
        direction: str,
        message_type: str,
        payload: dict[str, Any] | None,
        *,
        phase: str = "transport",
    ) -> int:
        frame_id = self.last + 1
        self.frames.append(
            RecordedFrame(
                id=frame_id,
                connection=1,
                client_id=CLIENT_ID,
                direction=direction,
                phase=phase,
                monotonic_us=frame_id,
                size=0,
                message={"type": message_type, "payload": payload or {}},
            )
        )
        return frame_id


def only(findings: list[Finding]) -> Finding:
    assert len(findings) == 1, findings
    return findings[0]


class TokenTests(unittest.TestCase):
    def test_reference_vector_names_its_client(self) -> None:
        findings = suite_checks.check_pairing_token(VECTOR_TOKEN, VECTOR_CLIENT_ID)
        self.assertEqual([f.verdict for f in findings], [Verdict.PASS, Verdict.PASS])

    def test_decoding_is_lenient(self) -> None:
        pasted = "  " + VECTOR_TOKEN.removeprefix("SP:").lower() + "\n"
        findings = suite_checks.check_pairing_token(pasted, VECTOR_CLIENT_ID)
        self.assertEqual([f.verdict for f in findings], [Verdict.PASS, Verdict.PASS])

    def test_token_for_another_client_fails(self) -> None:
        findings = suite_checks.check_pairing_token(VECTOR_TOKEN, CLIENT_ID)
        self.assertEqual([f.verdict for f in findings], [Verdict.PASS, Verdict.FAIL])
        self.assertIn(VECTOR_CLIENT_ID, findings[1].message)

    def test_malformed_token_fails(self) -> None:
        for token in ("SP:0ABC", "SP:1" + VECTOR_TOKEN[4:], "not a token"):
            with self.subTest(token=token):
                findings = suite_checks.check_pairing_token(token, VECTOR_CLIENT_ID)
                self.assertEqual(
                    [f.verdict for f in findings], [Verdict.FAIL, Verdict.NOT_APPLICABLE]
                )


class UnpairedTests(unittest.TestCase):
    def test_sentinel_handshake(self) -> None:
        check = suite_checks.check_sentinel_handshake
        self.assertIs(only(check(Connection().opening().record())).verdict, Verdict.PASS)
        lt = Connection().opening(keyed="long_term").record()
        self.assertIs(only(check(lt)).verdict, Verdict.TOOL_ERROR)
        failed = Connection().opening(keyed=None).record()
        self.assertIs(only(check(failed)).verdict, Verdict.FAIL)
        silent = Connection()
        silent.came("client/init", {"client_id": CLIENT_ID}, phase="cleartext")
        silent.out("noise/handshake", phase="cleartext")
        finding = only(check(silent.record()))
        self.assertIs(finding.verdict, Verdict.FAIL)
        self.assertIn("did not answer Noise message 1", finding.message)

    def forced_playback(self, *, unpaired: bool = False) -> Connection:
        connection = Connection().opening(unpaired=unpaired)
        connection.out("server/activate", {"activities": [], "active_roles": []})
        connection.out("server/activate", {"activities": ["playback"], "active_roles": ROLES})
        return connection

    def test_pairing_required(self) -> None:
        check = suite_checks.check_pairing_required
        good = self.forced_playback()
        good.came("client/goodbye", {"reason": "pairing_required"})
        self.assertIs(only(check(good.record())).verdict, Verdict.PASS)

        wrong = self.forced_playback()
        wrong.came("client/goodbye", {"reason": "unauthorized"})
        finding = only(check(wrong.record()))
        self.assertIs(finding.verdict, Verdict.FAIL)
        self.assertIn("'unauthorized', not 'pairing_required'", finding.message)

        accepted = self.forced_playback()
        accepted.came("client/state", {"available": True})
        self.assertIs(only(check(accepted.record())).verdict, Verdict.FAIL)

        dropped = self.forced_playback()
        dropped.event(EventKind.CLOSED)
        finding = only(check(dropped.record()))
        self.assertIs(finding.verdict, Verdict.FAIL)
        self.assertIn("closed the connection without", finding.message)

        enabled = self.forced_playback(unpaired=True)
        self.assertIs(only(check(enabled.record())).verdict, Verdict.NOT_APPLICABLE)

    def test_unpaired_playback(self) -> None:
        check = suite_checks.check_unpaired_playback

        def activated() -> Connection:
            connection = Connection().opening(unpaired=True)
            connection.out("server/activate", {"activities": ["playback"], "active_roles": ROLES})
            return connection

        good = activated()
        good.came("client/time", {"client_transmitted": 1})
        good.came("client/state", {"available": True})
        self.assertIs(only(check(good.record())).verdict, Verdict.PASS)
        refused = activated()
        refused.came("client/goodbye", {"reason": "pairing_required"})
        self.assertIs(only(check(refused.record())).verdict, Verdict.FAIL)
        self.assertIs(only(check(activated().record())).verdict, Verdict.FAIL)
        disabled = Connection().opening(unpaired=False).record()
        self.assertIs(only(check(disabled)).verdict, Verdict.NOT_APPLICABLE)

    def inadmissible(self, *, unpaired: bool) -> Connection:
        connection = Connection().opening(unpaired=unpaired)
        connection.out("server/activate", {"activities": [], "active_roles": []})
        connection.out("server/activate", {**PAIRING_ACTIVATION, "active_roles": []})
        return connection

    def test_inadmissible_activation_with_unpaired_access_off(self) -> None:
        check = suite_checks.check_inadmissible_activation
        good = self.inadmissible(unpaired=False)
        good.came("client/goodbye", {"reason": "unauthorized"})
        self.assertIs(only(check(good.record())).verdict, Verdict.PASS)
        for response in (
            ("client/goodbye", {"reason": "pairing_required"}),
            ("pair/abort", {"reason": "method_not_supported"}),
        ):
            with self.subTest(response=response):
                wrong = self.inadmissible(unpaired=False)
                wrong.came(*response)
                self.assertIs(only(check(wrong.record())).verdict, Verdict.FAIL)

    def test_inadmissible_activation_with_unpaired_access_on(self) -> None:
        check = suite_checks.check_inadmissible_activation
        good = self.inadmissible(unpaired=True)
        good.came("pair/abort", {"reason": "method_not_supported"})
        self.assertIs(only(check(good.record())).verdict, Verdict.PASS)
        closed = self.inadmissible(unpaired=True)
        closed.came("pair/abort", {"reason": "method_not_supported"})
        closed.event(EventKind.CLOSED)
        self.assertIs(only(check(closed.record())).verdict, Verdict.FAIL)
        goodbye = self.inadmissible(unpaired=True)
        goodbye.came("client/goodbye", {"reason": "unauthorized"})
        self.assertIs(only(check(goodbye.record())).verdict, Verdict.FAIL)


class PairingPskTests(unittest.TestCase):
    def verdicts(self, connection: Connection) -> dict[str, Verdict]:
        record = connection.record()
        since = next(f.id for f in record.frames if f.type == "client/hello")
        findings = [
            *suite_checks.check_pairing_rehandshake(record, since),
            *suite_checks.check_pair_init(record, since),
            *suite_checks.check_pair_finalize(record, since),
            *suite_checks.check_promotion(record, since),
            *suite_checks.check_paired_activation(record, since),
        ]
        return {f.id: f.verdict for f in findings}

    def test_complete_pairing_passes(self) -> None:
        verdicts = self.verdicts(Connection().opening().pairing())
        self.assertEqual(set(verdicts.values()), {Verdict.PASS}, verdicts)
        self.assertEqual(len(verdicts), 5)

    def test_client_without_its_pairing_psk_fails_the_rehandshake(self) -> None:
        connection = Connection().opening()
        connection.out("noise/handshake", {"data": "pr1"})
        connection.event(EventKind.CLOSED)
        verdicts = self.verdicts(connection)
        self.assertIs(verdicts["PAIR-PSK-003"], Verdict.FAIL)
        self.assertIs(verdicts["PAIR-PSK-004"], Verdict.NOT_APPLICABLE)

    def test_message_2_that_does_not_verify_fails(self) -> None:
        connection = Connection().opening()
        connection.out("noise/handshake", {"data": "pr1"})
        connection.came("noise/handshake", {"data": "bad"})
        self.assertIs(self.verdicts(connection)["PAIR-PSK-003"], Verdict.FAIL)

    def test_pair_init_problems_fail(self) -> None:
        for payload in ({"pairing_index": 2}, {"pairing_index": 1, "commit_B": "x"}):
            with self.subTest(payload=payload):
                connection = Connection().opening()
                connection.out("noise/handshake")
                connection.came("noise/handshake")
                connection.out("server/activate", PAIRING_ACTIVATION)
                connection.came("client/pair-init", payload)
                self.assertIs(self.verdicts(connection)["PAIR-PSK-004"], Verdict.FAIL)

    def test_abort_instead_of_pair_init_fails(self) -> None:
        connection = Connection().opening()
        connection.out("noise/handshake")
        connection.came("noise/handshake")
        connection.out("server/activate", PAIRING_ACTIVATION)
        connection.came("pair/abort", {"reason": "method_not_supported"})
        verdicts = self.verdicts(connection)
        self.assertIs(verdicts["PAIR-PSK-004"], Verdict.FAIL)
        self.assertIs(verdicts["PAIR-PSK-005"], Verdict.NOT_APPLICABLE)

    def test_finalize_problems_fail(self) -> None:
        for finalize in ({"wrapped_psk": "<redacted>"}, {"long_term_psk": 5}):
            with self.subTest(finalize=finalize):
                verdicts = self.verdicts(Connection().opening().pairing(finalize=finalize))
                self.assertIs(verdicts["PAIR-PSK-005"], Verdict.FAIL)

    def test_finalize_aiosendspin_refused_fails(self) -> None:
        connection = Connection().opening()
        connection.out("noise/handshake")
        connection.came("noise/handshake")
        connection.out("server/activate", PAIRING_ACTIVATION)
        connection.came("client/pair-init", {"pairing_index": 1})
        connection.came("client/pair-finalize", {"long_term_psk": "<redacted>"})
        connection.event(EventKind.CLOSED)
        verdicts = self.verdicts(connection)
        self.assertIs(verdicts["PAIR-PSK-005"], Verdict.FAIL)
        self.assertIs(verdicts["PAIR-PSK-006"], Verdict.NOT_APPLICABLE)

    def test_client_that_does_not_promote_fails(self) -> None:
        connection = Connection().opening()
        connection.out("noise/handshake")
        connection.came("noise/handshake")
        connection.out("server/activate", PAIRING_ACTIVATION)
        connection.came("client/pair-init", {"pairing_index": 1})
        connection.came("client/pair-finalize", {"long_term_psk": "<redacted>"})
        connection.out("server/pair-finalize")
        connection.out("noise/handshake", {"data": "lt1"})
        connection.event(EventKind.CLOSED)
        verdicts = self.verdicts(connection)
        self.assertIs(verdicts["PAIR-PSK-006"], Verdict.FAIL)
        self.assertIs(verdicts["PAIR-PSK-007"], Verdict.NOT_APPLICABLE)

    def test_refused_paired_activation_fails(self) -> None:
        refused = Connection().opening().pairing()
        refused.came("client/goodbye", {"reason": "unauthorized"})
        self.assertIs(self.verdicts(refused)["PAIR-PSK-007"], Verdict.FAIL)
        dropped = Connection().opening().pairing()
        dropped.event(EventKind.CLOSED)
        self.assertIs(self.verdicts(dropped)["PAIR-PSK-007"], Verdict.FAIL)


class PostPairingTests(unittest.TestCase):
    def test_long_term_reconnect(self) -> None:
        check = suite_checks.check_long_term_reconnect
        lt = Connection().opening(keyed="long_term").record()
        self.assertIs(only(check(lt)).verdict, Verdict.PASS)
        lost = Connection().opening(keyed="sentinel", mismatch=True).record()
        self.assertIs(only(check(lost)).verdict, Verdict.FAIL)
        unpaired = Connection().opening(keyed="sentinel").record()
        self.assertIs(only(check(unpaired)).verdict, Verdict.TOOL_ERROR)

    def test_forgotten_reconnect(self) -> None:
        check = suite_checks.check_forgotten_reconnect
        good = Connection().opening()
        good.out("server/activate", {"activities": [], "active_roles": []})
        self.assertIs(only(check(good.record())).verdict, Verdict.PASS)
        played = Connection().opening()
        played.out("server/activate", {"activities": ["playback"], "active_roles": ROLES})
        self.assertIs(only(check(played.record())).verdict, Verdict.TOOL_ERROR)
        refused = Connection().opening(keyed=None).record()
        self.assertIs(only(check(refused)).verdict, Verdict.FAIL)

    def unpaired(self, *, keyed: str = "long_term") -> Connection:
        connection = Connection().opening(keyed=keyed)
        connection.out("server/activate", {"activities": ["playback"], "active_roles": ROLES})
        connection.out("server/unpair")
        return connection

    def test_unpair_goodbye(self) -> None:
        check = suite_checks.check_unpair_goodbye
        good = self.unpaired()
        good.came("client/goodbye", {"reason": "unpaired"})
        self.assertIs(only(check(good.record())).verdict, Verdict.PASS)
        wrong = self.unpaired()
        wrong.came("client/goodbye", {"reason": "shutdown"})
        self.assertIs(only(check(wrong.record())).verdict, Verdict.FAIL)
        ignored = self.unpaired()
        ignored.came("client/state", {"available": True})
        self.assertIs(only(check(ignored.record())).verdict, Verdict.FAIL)
        sentinel = self.unpaired(keyed="sentinel").record()
        self.assertIs(only(check(sentinel)).verdict, Verdict.NOT_APPLICABLE)
        paired_since = Connection().opening().pairing()
        paired_since.out("server/unpair")
        paired_since.came("client/goodbye", {"reason": "unpaired"})
        self.assertIs(only(check(paired_since.record())).verdict, Verdict.PASS)

    def test_record_dropped(self) -> None:
        check = suite_checks.check_record_dropped
        dropped = Connection().opening(keyed="sentinel", mismatch=True).record()
        self.assertIs(only(check(dropped)).verdict, Verdict.PASS)
        kept = only(check(Connection().opening(keyed="long_term").record()))
        self.assertIs(kept.verdict, Verdict.FAIL)
        self.assertIn("still holds the pairing record", kept.message)

    def test_restart_keeps_pairing(self) -> None:
        check = suite_checks.check_restart_keeps_pairing
        kept = Connection().opening(keyed="long_term").record()
        self.assertIs(only(check(kept)).verdict, Verdict.PASS)
        lost = Connection().opening(keyed="sentinel", mismatch=True).record()
        self.assertIs(only(check(lost)).verdict, Verdict.FAIL)

    def test_restart_keeps_output_delay(self) -> None:
        check = suite_checks.check_restart_keeps_output_delay

        def reporting(*delays: int) -> ConnectionRecord:
            connection = Connection().opening(keyed="long_term")
            for delay in delays:
                connection.came("client/state", {"player": {"output_delay_ms": delay}})
            return connection.record()

        self.assertIs(only(check(reporting(0, 250), reporting(250))).verdict, Verdict.PASS)
        self.assertIs(only(check(reporting(250), reporting(0))).verdict, Verdict.FAIL)
        self.assertIs(only(check(reporting(), reporting(0))).verdict, Verdict.NOT_APPLICABLE)


class SpecRefTests(unittest.TestCase):
    def test_every_check_cites_an_rc1_file_and_line(self) -> None:
        for check_id, ref in suite_checks.SPEC_REFS.items():
            with self.subTest(check_id=check_id):
                self.assertRegex(ref, r"^[a-z/0-9]+\.md:\d+(-\d+)?(, [a-z/0-9]+\.md:\d+(-\d+)?)*$")


if __name__ == "__main__":
    unittest.main()
