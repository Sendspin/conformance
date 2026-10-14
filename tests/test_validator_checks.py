"""The validator's machine checks judge hand-built frame sequences correctly."""

import unittest
from typing import Any

from conformance.validator import checks
from conformance.validator.checks import ConnectionRecord, judge_session, run_checks
from conformance.validator.findings import Verdict
from conformance.validator.recorder import RecordedFrame, RecordedNoncompliance

CLIENT_ID = "IjlDhKVumKnfsZCkQ389bq2a6oHQ_y_xm84lYXJ3yCo"
GOOD_INIT = {"client_id": CLIENT_ID, "version": 1, "suite": "25519_ChaChaPoly_SHA256"}
GOOD_HELLO = {
    "name": "Kitchen",
    "device_info": {"manufacturer": "Acme"},
    "supported_roles": ["player@v1", "controller@v1", "_acme_lights@v1"],
    "player@v1_support": {"supported_formats": [], "buffer_capacity": 1},
    "_acme_lights@v1_support": {},
    "supported_pair_methods": {
        "pairing_psk": {"locations": ["device"]},
        "dynamic_pairing_code": {"out_channels": ["display"], "formats": ["digits", "qr_code"]},
    },
    "unpaired_access": {"enabled": True},
}
ACTIVE_ROLES = ["player@v1", "controller@v1"]
GOOD_STATE = {"available": True, "player": {"volume": 50}}


def frame(
    frame_id: int,
    direction: str,
    phase: str,
    message_type: str | None = None,
    payload: dict[str, Any] | None = None,
    **fields: Any,
) -> RecordedFrame:
    message = {"type": message_type, "payload": payload or {}} if message_type else None
    return RecordedFrame(
        id=frame_id,
        connection=1,
        client_id=CLIENT_ID,
        direction=direction,
        phase=phase,
        monotonic_us=frame_id,
        size=0,
        message=fields.pop("message", message),
        **fields,
    )


def session(**replace: Any) -> list[tuple[str, str, str, Any]]:
    """
    Return a known-good opening exchange as (direction, phase, type, payload) steps.

    A keyword named after a step's type replaces that step's payload; None drops it.
    """
    steps = [
        ("in", "cleartext", "client/init", GOOD_INIT),
        ("out", "cleartext", "server/init", {"server_id": CLIENT_ID, "version": 1}),
        ("out", "cleartext", "noise/handshake#1", {"data": "x"}),
        ("in", "cleartext", "noise/handshake#2", {"data": "y"}),
        ("out", "transport", "server/hello", {"name": "Sendspin Validator"}),
        ("in", "transport", "client/hello", GOOD_HELLO),
        (
            "out",
            "transport",
            "server/activate",
            {"activities": ["playback"], "active_roles": ACTIVE_ROLES},
        ),
        ("in", "transport", "client/state", GOOD_STATE),
        ("in", "transport", "client/time", {"client_transmitted": 123}),
    ]
    result = []
    for direction, phase, key, default in steps:
        name = key.replace("/", "_").replace("-", "_").replace("#", "_")
        payload = replace.get(name, default)
        if name in replace and payload is None:
            continue
        result.append((direction, phase, key.split("#")[0], payload))
    return result


def record(
    steps: list[tuple[str, str, str, Any]],
    noncompliance: tuple[RecordedNoncompliance, ...] = (),
) -> ConnectionRecord:
    frames = tuple(
        frame(i, direction, phase, message_type, payload)
        for i, (direction, phase, message_type, payload) in enumerate(steps, start=1)
    )
    return ConnectionRecord(1, frames, noncompliance)


def rejection(reason: str, after_frame: int = 1) -> RecordedNoncompliance:
    return RecordedNoncompliance(1, CLIENT_ID, reason, 0, after_frame)


class CheckTestCase(unittest.TestCase):
    def verdict(self, check: Any, connection: ConnectionRecord) -> Verdict:
        [finding] = check(connection)
        return finding.verdict

    def assert_verdict(self, check: Any, connection: ConnectionRecord, expected: Verdict) -> None:
        [finding] = check(connection)
        self.assertIs(finding.verdict, expected, finding.message)


class KnownGoodSessionTests(CheckTestCase):
    def test_every_check_passes_or_does_not_apply(self) -> None:
        findings = run_checks(record(session()), txt_name="Kitchen")
        self.assertEqual({f.id: f.verdict for f in findings if f.verdict is not Verdict.PASS}, {})
        self.assertEqual({f.id for f in findings}, {*checks.CHECKS, "CORE-HELLO-007"})

    def test_every_finding_cites_the_spec(self) -> None:
        for finding in run_checks(record(session(client_init={"version": 2}))):
            self.assertRegex(finding.spec_ref, r"^[a-z/0-9]+\.md:\d+")


class ClientInitTests(CheckTestCase):
    def test_first_frame_must_be_client_init(self) -> None:
        steps = session(client_init=None)
        steps.insert(0, ("in", "cleartext", "client/hello", {}))
        connection = record(steps)
        self.assert_verdict(checks.check_opens_with_client_init, connection, Verdict.FAIL)
        self.assert_verdict(checks.check_client_id, connection, Verdict.NOT_APPLICABLE)

    def test_client_id(self) -> None:
        self.assert_verdict(checks.check_client_id, record(session()), Verdict.PASS)
        for bad in (CLIENT_ID + "=", CLIENT_ID[:42], CLIENT_ID[:42] + "+", 7):
            with self.subTest(client_id=bad):
                connection = record(session(client_init={**GOOD_INIT, "client_id": bad}))
                self.assert_verdict(checks.check_client_id, connection, Verdict.FAIL)

    def test_version(self) -> None:
        self.assert_verdict(checks.check_init_version, record(session()), Verdict.PASS)
        for bad in (2, "1", True, None):
            with self.subTest(version=bad):
                connection = record(session(client_init={**GOOD_INIT, "version": bad}))
                self.assert_verdict(checks.check_init_version, connection, Verdict.FAIL)

    def test_suite(self) -> None:
        aes = record(session(client_init={**GOOD_INIT, "suite": "25519_AESGCM_SHA256"}))
        self.assert_verdict(checks.check_init_suite, aes, Verdict.PASS)
        bad = record(session(client_init={**GOOD_INIT, "suite": "448_ChaChaPoly_BLAKE2b"}))
        self.assert_verdict(checks.check_init_suite, bad, Verdict.FAIL)

    def test_undefined_fields(self) -> None:
        self.assert_verdict(checks.check_init_fields, record(session()), Verdict.PASS)
        for extra in ({"name": "x"}, {"_custom": 1}):
            with self.subTest(extra=extra):
                connection = record(session(client_init={**GOOD_INIT, **extra}))
                self.assert_verdict(checks.check_init_fields, connection, Verdict.FAIL)


class NoiseTests(CheckTestCase):
    def test_completed_handshake_passes(self) -> None:
        self.assert_verdict(checks.check_noise_message_2, record(session()), Verdict.PASS)

    def test_strict_rejection_of_the_payload_fails(self) -> None:
        steps = session()[:4]
        reason = "Noise message 2 payload is not the literal bytes {}"
        connection = record(steps, (rejection(reason, after_frame=4),))
        self.assert_verdict(checks.check_noise_message_2, connection, Verdict.FAIL)

    def test_unseen_payload_is_a_tool_error(self) -> None:
        self.assert_verdict(checks.check_noise_message_2, record(session()[:4]), Verdict.TOOL_ERROR)
        self.assert_verdict(checks.check_noise_message_2, record(session()[:3]), Verdict.TOOL_ERROR)

    def test_rejected_init_does_not_apply(self) -> None:
        steps = [session()[0], ("out", "cleartext", "server/error", {"reason": "malformed"})]
        self.assert_verdict(checks.check_noise_message_2, record(steps), Verdict.NOT_APPLICABLE)


class OrderTests(CheckTestCase):
    def test_hello_and_goodbye_may_precede_activation(self) -> None:
        steps = session()
        steps.insert(6, ("in", "transport", "client/goodbye", {"reason": "shutdown"}))
        self.assert_verdict(checks.check_nothing_before_activation, record(steps), Verdict.PASS)

    def test_any_other_message_before_activation_fails(self) -> None:
        steps = session()
        steps.insert(6, ("in", "transport", "client/state", GOOD_STATE))
        self.assert_verdict(checks.check_nothing_before_activation, record(steps), Verdict.FAIL)

    def test_a_message_without_any_activation_fails(self) -> None:
        steps = session(server_activate=None)
        self.assert_verdict(checks.check_nothing_before_activation, record(steps), Verdict.FAIL)

    def test_binary_message_before_activation_fails(self) -> None:
        connection = record(session()[:6])
        early = frame(7, "in", "transport", binary_type=8)
        connection = ConnectionRecord(1, (*connection.frames, early))
        self.assert_verdict(checks.check_nothing_before_activation, connection, Verdict.FAIL)

    def test_no_transport_does_not_apply(self) -> None:
        self.assert_verdict(
            checks.check_nothing_before_activation, record(session()[:4]), Verdict.NOT_APPLICABLE
        )


class HelloTests(CheckTestCase):
    def hello(self, **changes: Any) -> ConnectionRecord:
        payload = {**GOOD_HELLO, **changes}
        return record(session(client_hello={k: v for k, v in payload.items() if v is not None}))

    def test_hello_must_follow_server_hello(self) -> None:
        self.assert_verdict(checks.check_hello_sent, record(session()), Verdict.PASS)
        silent = record(session(client_hello=None)[:5])
        self.assert_verdict(checks.check_hello_sent, silent, Verdict.FAIL)
        rejected = record(session(client_hello=None)[:5], (rejection("x"),))
        self.assert_verdict(checks.check_hello_sent, rejected, Verdict.NOT_APPLICABLE)

    def test_support_objects(self) -> None:
        self.assert_verdict(checks.check_hello_support_objects, self.hello(), Verdict.PASS)
        missing = self.hello(**{"player@v1_support": None})
        self.assert_verdict(checks.check_hello_support_objects, missing, Verdict.FAIL)
        unlisted = self.hello(**{"visualizer@v1_support": {}})
        self.assert_verdict(checks.check_hello_support_objects, unlisted, Verdict.FAIL)

    def test_roles(self) -> None:
        self.assert_verdict(checks.check_hello_roles, self.hello(), Verdict.PASS)
        for roles in (["player"], ["player@v1", "@v1"], ["player v1@v1"], "player@v1"):
            with self.subTest(roles=roles):
                connection = self.hello(supported_roles=roles)
                self.assert_verdict(checks.check_hello_roles, connection, Verdict.FAIL)

    def test_pair_methods(self) -> None:
        self.assert_verdict(checks.check_hello_pair_methods, self.hello(), Verdict.PASS)
        reserved = self.hello(supported_pair_methods={"pairing_psk": {}, "future_method": 1})
        self.assert_verdict(checks.check_hello_pair_methods, reserved, Verdict.PASS)
        bad_methods = {
            "missing": None,
            "list": ["pairing_psk"],
            "no pairing_psk": {"static_pairing_code": {}},
            "two code methods": {
                "pairing_psk": {},
                "static_pairing_code": {},
                "dynamic_pairing_code": {"out_channels": ["display"], "formats": ["digits"]},
            },
            "descriptor not an object": {"pairing_psk": []},
            "undefined descriptor field": {"pairing_psk": {"hint": "x"}},
            "locations not strings": {"pairing_psk": {"locations": [1]}},
            "no formats": {"pairing_psk": {}, "dynamic_pairing_code": {"out_channels": []}},
            "empty formats": {
                "pairing_psk": {},
                "dynamic_pairing_code": {"out_channels": ["display"], "formats": []},
            },
            "qr without display": {
                "pairing_psk": {},
                "dynamic_pairing_code": {"out_channels": ["speaker"], "formats": ["qr_code"]},
            },
        }
        for case, methods in bad_methods.items():
            with self.subTest(case):
                connection = self.hello(supported_pair_methods=methods)
                self.assert_verdict(checks.check_hello_pair_methods, connection, Verdict.FAIL)

    def test_unpaired_access(self) -> None:
        self.assert_verdict(checks.check_hello_unpaired_access, self.hello(), Verdict.PASS)
        for access in (None, {}, {"enabled": "yes"}, True):
            with self.subTest(access=access):
                connection = self.hello(unpaired_access=access)
                self.assert_verdict(checks.check_hello_unpaired_access, connection, Verdict.FAIL)

    def test_undefined_fields(self) -> None:
        self.assert_verdict(checks.check_hello_fields, self.hello(), Verdict.PASS)
        custom = self.hello(_acme_extra={"x": 1})
        self.assert_verdict(checks.check_hello_fields, custom, Verdict.PASS)
        for case, changes in {
            "top level": {"version": 1},
            "unversioned support": {"player_support": {}},
            "role without a support object": {"controller@v1_support": {}},
            "device_info": {"device_info": {"serial": "1"}},
            "unpaired_access": {"unpaired_access": {"enabled": True, "pin": "1"}},
        }.items():
            with self.subTest(case):
                self.assert_verdict(checks.check_hello_fields, self.hello(**changes), Verdict.FAIL)

    def test_name_matches_txt(self) -> None:
        connection = self.hello()
        self.assertIs(
            self.verdict(lambda c: checks.check_hello_name_matches_txt(c, "Kitchen"), connection),
            Verdict.PASS,
        )
        self.assertIs(
            self.verdict(lambda c: checks.check_hello_name_matches_txt(c, "Lounge"), connection),
            Verdict.WARN,
        )
        self.assertIs(
            self.verdict(lambda c: checks.check_hello_name_matches_txt(c, None), connection),
            Verdict.NOT_APPLICABLE,
        )

    def test_hello_checks_without_a_hello_do_not_apply(self) -> None:
        connection = record(session(client_hello=None))
        for check in (
            checks.check_hello_support_objects,
            checks.check_hello_roles,
            checks.check_hello_pair_methods,
            checks.check_hello_unpaired_access,
            checks.check_hello_fields,
        ):
            with self.subTest(check.__name__):
                self.assert_verdict(check, connection, Verdict.NOT_APPLICABLE)


class StateTests(CheckTestCase):
    def test_initial_state(self) -> None:
        self.assert_verdict(checks.check_initial_state_sent, record(session()), Verdict.PASS)
        self.assert_verdict(
            checks.check_initial_state_sent, record(session(client_state=None)), Verdict.FAIL
        )

    def test_initial_state_without_roles_does_not_apply(self) -> None:
        connection = record(
            session(server_activate={"activities": [], "active_roles": []}, client_state=None)
        )
        self.assert_verdict(checks.check_initial_state_sent, connection, Verdict.NOT_APPLICABLE)

    def test_initial_state_not_reached_does_not_apply(self) -> None:
        rejected = record(session(client_state=None), (rejection("x"),))
        self.assert_verdict(checks.check_initial_state_sent, rejected, Verdict.NOT_APPLICABLE)
        steps = session(client_state=None)
        steps.append(("in", "transport", "client/goodbye", {"reason": "shutdown"}))
        self.assert_verdict(checks.check_initial_state_sent, record(steps), Verdict.NOT_APPLICABLE)

    def test_available(self) -> None:
        self.assert_verdict(checks.check_state_available, record(session()), Verdict.PASS)
        for state in ({"player": {}}, {"available": "true"}):
            with self.subTest(state=state):
                connection = record(session(client_state=state))
                self.assert_verdict(checks.check_state_available, connection, Verdict.FAIL)

    def test_role_objects(self) -> None:
        self.assert_verdict(checks.check_state_role_objects, record(session()), Verdict.PASS)
        artwork = record(session(client_state={**GOOD_STATE, "artwork": {"channels": []}}))
        self.assert_verdict(checks.check_state_role_objects, artwork, Verdict.FAIL)

    def test_role_objects_count_every_earlier_activation(self) -> None:
        steps = session()
        steps.insert(7, ("out", "transport", "server/activate", {"active_roles": []}))
        self.assert_verdict(checks.check_state_role_objects, record(steps), Verdict.PASS)


class TimeTests(CheckTestCase):
    def test_client_transmitted(self) -> None:
        self.assert_verdict(checks.check_time_integer, record(session()), Verdict.PASS)
        for value in (1.5, "1", True, None):
            with self.subTest(value=value):
                connection = record(session(client_time={"client_transmitted": value}))
                self.assert_verdict(checks.check_time_integer, connection, Verdict.FAIL)
        self.assert_verdict(
            checks.check_time_integer, record(session(client_time=None)), Verdict.NOT_APPLICABLE
        )


class SessionTests(unittest.TestCase):
    def frames(self) -> list[RecordedFrame]:
        return list(record(session()).frames)

    def test_strict_rejection_fails_with_its_spec_reference(self) -> None:
        reason = "sent a message before the first server/activate"
        findings = judge_session(
            self.frames(), [rejection(reason, after_frame=7)], [], connection=1
        )
        [strict] = [f for f in findings if f.id == checks.STRICT_CHECK_ID]
        self.assertIs(strict.verdict, Verdict.FAIL)
        self.assertEqual(strict.spec_ref, "messaging.md:14")
        self.assertEqual(strict.frames, (7,))

    def test_no_rejection_passes(self) -> None:
        findings = judge_session(self.frames(), [], [], connection=1)
        [strict] = [f for f in findings if f.id == checks.STRICT_CHECK_ID]
        self.assertIs(strict.verdict, Verdict.PASS)

    def test_rejection_rc1_does_not_make_is_a_tool_error(self) -> None:
        for reason in ("sent an unsolicited management/result", "a reason nobody mapped"):
            with self.subTest(reason=reason):
                findings = judge_session(self.frames(), [rejection(reason)], [], connection=1)
                [strict] = [f for f in findings if f.id == checks.STRICT_CHECK_ID]
                self.assertIs(strict.verdict, Verdict.TOOL_ERROR)

    def test_recorder_errors_are_tool_errors(self) -> None:
        findings = judge_session(self.frames(), [], ["could not record frame 3"], connection=1)
        [recorder] = [f for f in findings if f.id == checks.RECORDER_CHECK_ID]
        self.assertIs(recorder.verdict, Verdict.TOOL_ERROR)

    def test_a_check_that_raises_is_a_tool_error(self) -> None:
        def broken(_: ConnectionRecord) -> list:
            raise KeyError("boom")

        original = checks.CHECKS["CORE-TIME-001"]
        checks.CHECKS["CORE-TIME-001"] = broken
        try:
            findings = run_checks(record(session()))
        finally:
            checks.CHECKS["CORE-TIME-001"] = original
        [time] = [f for f in findings if f.id == "CORE-TIME-001"]
        self.assertIs(time.verdict, Verdict.TOOL_ERROR)

    def test_other_connections_are_not_judged(self) -> None:
        stray = [
            RecordedFrame(**{**vars_of(f), "id": f.id + 100, "connection": 2})
            for f in record(session(client_init={"version": 2})).frames
        ]
        flagged = RecordedNoncompliance(
            2, "stray", "sent a message before the first server/activate", 0, 101
        )
        findings = judge_session(self.frames() + stray, [flagged], [], connection=1)
        self.assertEqual([f for f in findings if f.verdict is Verdict.FAIL], [])
        self.assertEqual(len([f for f in findings if f.id == "CORE-INIT-001"]), 1)

    def test_noise_rejection_is_reported_once(self) -> None:
        steps = session()[:4]
        flagged = rejection(checks.NOISE_PAYLOAD_REASON, after_frame=4)
        findings = judge_session(list(record(steps).frames), [flagged], [], connection=1)
        self.assertEqual([f.id for f in findings if f.verdict is Verdict.FAIL], ["CORE-NOISE-001"])


def vars_of(recorded: RecordedFrame) -> dict[str, Any]:
    return {name: getattr(recorded, name) for name in RecordedFrame.__slots__}


if __name__ == "__main__":
    unittest.main()
