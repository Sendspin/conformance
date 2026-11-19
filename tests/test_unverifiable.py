"""Missing SDK evidence does not skip execution or excuse observed failures."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from conformance.models import UnverifiableReason
from conformance.protocol import SPEC_REVISION, protocol_evidence_failure
from conformance.runner import LaunchSpec, _compare_summaries, run_case
from conformance.scenarios import require_scenario
from conformance.site import _display_status, _status_counts, _status_pill_row
from test_chunk_framing import _client_summary, _server_summary


class UnverifiableComparisonTest(unittest.TestCase):
    def test_unobserved_frames_do_not_pass(self) -> None:
        matches, reason = self.compare()
        self.assertFalse(matches)
        self.assertIsInstance(reason, UnverifiableReason)

    def test_bad_server_header_still_fails(self) -> None:
        matches, reason = self.compare(server=_server_summary([25], header_bytes=9))
        self.assertFalse(matches)
        self.assertNotIsInstance(reason, UnverifiableReason)
        self.assertIn("header of 9 bytes", reason)

    def test_invalid_group_id_still_fails(self) -> None:
        server = copy.deepcopy(_server_summary([25]))
        server["group_updates"][0]["payload"]["group_id"] = "1"
        matches, reason = self.compare(server=server)
        self.assertFalse(matches)
        self.assertNotIsInstance(reason, UnverifiableReason)
        self.assertIn("43 base64url characters", reason)

    def test_time_violation_still_fails(self) -> None:
        server = _server_summary([25])
        server["time_exchange"] = [{"type": "server/time", "payload": {}}]
        matches, reason = self.compare(server=server)
        self.assertFalse(matches)
        self.assertNotIsInstance(reason, UnverifiableReason)
        self.assertIn("server/time", reason)

    def test_missing_or_malformed_frame_field_is_a_harness_failure(self) -> None:
        for value in ("invalid", [{}]):
            client = _client_summary([25], received_chunk_frames=value)
            _, reason = self.compare(client=client)
            self.assertNotIsInstance(reason, UnverifiableReason)
        client = _client_summary([25])
        del client["audio"]["received_chunk_frames"]
        _, reason = self.compare(client=client)
        self.assertNotIsInstance(reason, UnverifiableReason)

    def test_wrong_audio_total_still_fails_without_frames(self) -> None:
        client = _client_summary([25], observes_frames=False, received_encoded_byte_count=0)
        matches, reason = self.compare(client=client)
        self.assertFalse(matches)
        self.assertNotIsInstance(reason, UnverifiableReason)

    def test_other_peer_protocol_failure_survives_unavailable_evidence(self) -> None:
        reason = protocol_evidence_failure(
            {"protocol": None},
            {"protocol": {"spec_revision": SPEC_REVISION, "assertions": {
                "CORE-001": {"status": "failed", "detail": "wrong handshake order"},
            }}},
            assertion_ids=("CORE-001",),
        )
        self.assertNotIsInstance(reason, UnverifiableReason)
        self.assertIn("wrong handshake order", reason)

    def compare(self, *, server=None, client=None):
        return _compare_summaries(
            require_scenario("server-initiated-audio-chunk-framing"),
            server if server is not None else _server_summary([25]),
            client if client is not None else _client_summary([25], observes_frames=False),
        )


ADAPTER = """
import json, sys
ready, summary, payload, exit_code = sys.argv[1:]
json.dump({"status": "ready"}, open(ready, "w"))
json.dump(json.loads(payload), open(summary, "w"))
sys.exit(int(exit_code))
"""


class UnverifiableRunTest(unittest.IsolatedAsyncioTestCase):
    async def run_adapters(self, *, client_status="ok", client_exit=0):
        with tempfile.TemporaryDirectory() as directory:
            launched = []

            def launch(implementation, role, *, summary, ready, **kwargs):
                launched.append(role)
                payload = _server_summary([25]) if role == "server" else _client_summary(
                    [25], observes_frames=False
                )
                if role == "client":
                    payload["status"] = client_status
                    payload["reason"] = "connection closed before handshake"
                return LaunchSpec(
                    [sys.executable, "-c", ADAPTER, str(ready), str(summary),
                     json.dumps(payload), str(client_exit if role == "client" else 0)],
                    Path(directory),
                )

            with mock.patch("conformance.runner._build_role_command", side_effect=launch), mock.patch(
                "conformance.runner.fixture_path", return_value=Path("fixture.flac")
            ):
                result = await run_case(
                    results_dir=Path(directory),
                    scenario_id="server-initiated-audio-chunk-framing",
                    server_impl="aiosendspin", client_impl="sendspin-js",
                    timeout_s=5, slot_index=0,
                )
            self.assertEqual(launched, ["server", "client"])
            saved = json.loads((Path(result.case_dir) / "result.json").read_text())
            self.assertEqual(saved["status"], result.status)
            return result

    async def test_runs_both_adapters_before_reporting_unverifiable(self) -> None:
        result = await self.run_adapters()
        self.assertEqual(result.status, "unverifiable")
        self.assertEqual((result.server_exit_code, result.client_exit_code), (0, 0))

    async def test_connection_failure_stays_failed(self) -> None:
        result = await self.run_adapters(client_status="error", client_exit=1)
        self.assertEqual(result.status, "failed")
        self.assertIn("connection closed", result.reason)

    async def test_nonzero_exit_stays_failed(self) -> None:
        result = await self.run_adapters(client_exit=1)
        self.assertEqual(result.status, "failed")

    def test_report_keeps_unverifiable_separate(self) -> None:
        rows = [{"status": status} for status in ("passed", "failed", "unverifiable")]
        counts = _status_counts(rows)
        self.assertEqual(counts, {"passed": 1, "failed": 1, "unverifiable": 1})
        self.assertEqual(_display_status(rows[-1]), "unverifiable")
        self.assertIn("1 unverifiable", _status_pill_row(counts))
