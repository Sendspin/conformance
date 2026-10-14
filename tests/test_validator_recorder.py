"""The validator's recorder decodes frames and redacts secrets as it records them."""

import io
import json
import tempfile
import unittest
from pathlib import Path

from conformance._vendor.aiosendspin.server import (
    FrameDirection,
    FramePhase,
    ObservedFrame,
    ObservedNoncompliance,
)
from conformance.validator.recorder import REDACTED, EventKind, Recorder

SECRETS = {
    "long_term_psk": "LTPSK-secret-value",
    "wrapped_psk": "WRAPPED-psk-value",
    "wrapped_nonce_B": "WRAPPED-nonce-value",
    "pake_msg_1": "PAKE-one-value",
    "pake_msg_2": "PAKE-two-value",
    "server_kc": "SERVER-kc-value",
    "client_kc": "CLIENT-kc-value",
}
TOKEN = "SP:0ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def observed(
    data: bytes,
    *,
    phase: FramePhase = FramePhase.TRANSPORT,
    direction: FrameDirection = FrameDirection.IN,
    error: str | None = None,
) -> ObservedFrame:
    return ObservedFrame(
        connection=1,
        client_id="client",
        direction=direction,
        phase=phase,
        data=data,
        monotonic_us=10,
        error=error,
    )


def json_frame(message: object) -> ObservedFrame:
    return observed(b"\x00" + json.dumps(message).encode())


class RedactionTests(unittest.TestCase):
    """Secrets never outlive the frame: not in memory, `--verbose` output or the log."""

    def setUp(self) -> None:
        self.verbose = io.StringIO()
        self.recorder = Recorder(verbose=self.verbose)
        self.recorder.add_secret(TOKEN)
        self.recorder.observe_frame(
            json_frame(
                {
                    "type": "client/pair-finalize",
                    "payload": {
                        **SECRETS,
                        "nested": [{"pake_msg_3": "PAKE-three-value"}],
                        "message": f"token {TOKEN} entered",
                        "pairing_index": 1,
                    },
                }
            )
        )
        self.recorder.observe_noncompliance(
            ObservedNoncompliance(
                connection=1, client_id="client", reason=f"bad {TOKEN}", monotonic_us=11
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "protocol.log.jsonl"
            self.recorder.write_log(log)
            self.log = log.read_text()

    def test_secret_fields_are_redacted_when_recorded(self) -> None:
        payload = self.recorder.frames[0].payload
        assert payload is not None
        for key in SECRETS:
            self.assertEqual(payload[key], REDACTED, key)
        self.assertEqual(payload["nested"], [{"pake_msg_3": REDACTED}])
        self.assertEqual(payload["pairing_index"], 1)

    def test_registered_secrets_are_redacted_anywhere(self) -> None:
        payload = self.recorder.frames[0].payload
        assert payload is not None
        self.assertEqual(payload["message"], f"token {REDACTED} entered")
        self.assertEqual(self.recorder.noncompliance[0].reason, f"bad {REDACTED}")

    def test_no_secret_reaches_any_output(self) -> None:
        recorded = repr(self.recorder.frames) + repr(self.recorder.noncompliance)
        for secret in [*SECRETS.values(), "PAKE-three-value", TOKEN]:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, recorded)
                self.assertNotIn(secret, self.verbose.getvalue())
                self.assertNotIn(secret, self.log)


class DecodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = Recorder()

    def test_frames_are_numbered_in_order(self) -> None:
        self.recorder.observe_frame(
            observed(b'{"type": "client/init", "payload": {}}', phase=FramePhase.CLEARTEXT)
        )
        self.recorder.observe_frame(json_frame({"type": "client/hello", "payload": {}}))
        self.assertEqual([f.id for f in self.recorder.frames], [1, 2])
        self.assertEqual([f.type for f in self.recorder.frames], ["client/init", "client/hello"])

    def test_binary_frames_keep_only_type_and_size(self) -> None:
        self.recorder.observe_frame(observed(b"\x08secret audio"))
        [recorded] = self.recorder.frames
        self.assertEqual((recorded.binary_type, recorded.size, recorded.message), (8, 13, None))
        self.assertNotIn("secret", json.dumps(recorded.to_json()))

    def test_undecodable_frames_carry_an_error(self) -> None:
        for data in (b"\x00not json", b"\x00\xff", b"\x00[1]"):
            with self.subTest(data=data):
                self.recorder.observe_frame(observed(data))
                self.assertIsNotNone(self.recorder.frames[-1].error)
                self.assertIsNone(self.recorder.frames[-1].message)

    def test_frames_aiosendspin_failed_keep_its_error(self) -> None:
        self.recorder.observe_frame(observed(b"ciphertext", error="decrypt failed"))
        self.assertEqual(self.recorder.frames[0].error, "decrypt failed")

    def test_noncompliance_points_at_the_last_frame_of_its_connection(self) -> None:
        self.recorder.observe_frame(json_frame({"type": "client/hello", "payload": {}}))
        self.recorder.observe_noncompliance(
            ObservedNoncompliance(connection=1, client_id=None, reason="x", monotonic_us=1)
        )
        self.assertEqual(self.recorder.noncompliance[0].after_frame, 1)

    def test_events_point_at_the_last_frame_of_their_connection(self) -> None:
        verbose = io.StringIO()
        recorder = Recorder(verbose=verbose)
        recorder.observe_frame(json_frame({"type": "client/hello", "payload": {}}))
        recorder.observe_event(1, "client", EventKind.KEYED, psk_category="long_term")
        recorder.observe_event(2, "client", EventKind.CLOSED)
        [keyed, closed] = recorder.events
        self.assertEqual((keyed.after_frame, keyed.psk_category), (1, "long_term"))
        self.assertEqual((closed.after_frame, closed.psk_category), (None, None))
        self.assertIn("c1 keyed long_term", verbose.getvalue())

    def test_a_recorder_failure_is_kept_not_raised(self) -> None:
        frame = observed(b"\x00{}")
        object.__setattr__(frame, "data", None)
        self.recorder.observe_frame(frame)
        self.assertEqual(self.recorder.frames, [])
        self.assertEqual(len(self.recorder.tool_errors), 1)

    def test_verbose_output_escapes_terminal_controls(self) -> None:
        verbose = io.StringIO()
        recorder = Recorder(verbose=verbose)
        recorder.observe_frame(json_frame({"type": "client/hello", "payload": {"name": "\x1b[2J"}}))
        self.assertNotIn("\x1b", verbose.getvalue())


if __name__ == "__main__":
    unittest.main()
