"""Coverage for the negotiated-depth rule behind `server-initiated-pcm-24bit`.

The canonical PCM hash is taken over float samples, so a 16-bit stream of the
clip matches it as well as a 24-bit one does. The scenario therefore also reads
the negotiated bit depth, and a pair that streamed anything else has not tested
the 24-bit path. `roles/player/v1.md` requires no bit depth of a player, so the
reason must name a harness gap or the server's selection, never a client defect.
Reproducing each over the wire needs an adapter built to misbehave, so the rule
is exercised here against synthetic summaries.
"""

from __future__ import annotations

import unittest
from typing import Any

from conformance.declared_formats import stream_bit_depth_violation
from conformance.runner import _compare_summaries
from conformance.scenarios import SCENARIOS, require_scenario

PCM_16 = {"codec": "pcm", "sample_rate": 8000, "bit_depth": 16, "channels": 1}
PCM_24 = {"codec": "pcm", "sample_rate": 8000, "bit_depth": 24, "channels": 1}

SOURCE_HASH = "a" * 64

# Every server summary carries one, and a case whose summary lacks it never passes.
GROUP_UPDATE = {
    "type": "group/update",
    "payload": {"playback_state": "stopped", "group_id": "group-1", "group_name": "Kitchen"},
}

# What a server that held stream/start until the client reported available records.
AVAILABILITY_TRACE = [
    {"type": "client/state", "available": True},
    {"type": "stream/start", "phase": "sending", "roles": ["player"]},
    {"type": "stream/start", "phase": "sent", "roles": ["player"]},
]


def _server_summary(*, declared: Any, stream: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "status": "ok",
        "implementation": "synthetic-server",
        "role": "server",
        "group_updates": [GROUP_UPDATE],
        "time_exchange": [],
        "availability_trace": AVAILABILITY_TRACE,
        "peer_hello": {
            "type": "client/hello",
            "payload": {"player@v1_support": {"supported_formats": declared}},
        },
        "stream": stream,
        "audio": {"source_pcm_sha256": SOURCE_HASH, "channels": 1, "frame_count": 1000},
    }


def _client_summary(*, stream: dict[str, Any] | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "status": "ok",
        "implementation": "synthetic-client",
        "role": "client",
        "audio": {
            "received_pcm_sha256": SOURCE_HASH,
            "received_sample_count": 1000,
            "audio_chunk_count": 10,
        },
    }
    if stream is not None:
        summary["stream"] = stream
    return summary


class StreamBitDepthViolationTests(unittest.TestCase):
    """A case passes the rule only when the stream carried the depth under test."""

    def test_a_24_bit_stream_seen_by_both_sides_passes(self) -> None:
        self.assertIsNone(
            self._violation(
                _server_summary(declared=[PCM_24], stream=PCM_24),
                _client_summary(stream=PCM_24),
            )
        )

    def test_a_client_reporting_no_stream_is_not_judged(self) -> None:
        self.assertIsNone(
            self._violation(_server_summary(declared=[PCM_24], stream=PCM_24), _client_summary())
        )

    def test_an_undeclared_depth_is_a_harness_gap_not_a_client_defect(self) -> None:
        reason = self._violation(
            _server_summary(declared=[PCM_16], stream=PCM_16),
            _client_summary(stream=PCM_16),
        )
        assert reason is not None
        self.assertTrue(reason.startswith("Harness gap: the client adapter advertised no 24-bit"))
        self.assertIn("the stream was PCM · mono · 8 kHz · 16-bit", reason)
        self.assertIn("declared: PCM · mono · 8 kHz · 16-bit", reason)
        self.assertNotIn("support", reason)

    def test_a_server_that_streamed_another_depth_than_offered_is_named(self) -> None:
        reason = self._violation(
            _server_summary(declared=[PCM_24, PCM_16], stream=PCM_16),
            _client_summary(stream=PCM_16),
        )
        assert reason is not None
        self.assertTrue(reason.startswith("Server streamed PCM · mono · 8 kHz · 16-bit"))
        self.assertNotIn("Harness gap", reason)

    def test_a_client_that_observed_another_depth_is_named(self) -> None:
        reason = self._violation(
            _server_summary(declared=[PCM_24], stream=PCM_24),
            _client_summary(stream=PCM_16),
        )
        assert reason is not None
        self.assertTrue(reason.startswith("Client observed a PCM · mono · 8 kHz · 16-bit stream"))

    def test_a_server_summary_without_a_stream_fails_closed(self) -> None:
        reason = self._violation(
            _server_summary(declared=[PCM_24], stream=None),
            _client_summary(stream=PCM_24),
        )
        assert reason is not None
        self.assertTrue(reason.startswith("Harness gap: the server adapter recorded no stream"))

    def test_an_unrecorded_declaration_still_fails_a_16_bit_stream(self) -> None:
        server = _server_summary(declared=[PCM_16], stream=PCM_16)
        server["peer_hello"] = None
        reason = self._violation(server, _client_summary())
        assert reason is not None
        self.assertTrue(reason.startswith("Harness gap: the stream was PCM"))
        self.assertNotIn("client adapter", reason)
        self.assertIn("declared: none recorded", reason)

    def test_an_unreadable_declaration_names_neither_side(self) -> None:
        reason = self._violation(
            _server_summary(declared=[PCM_16, {"codec": "pcm"}], stream=PCM_16),
            _client_summary(),
        )
        assert reason is not None
        self.assertTrue(reason.startswith("Harness gap: the stream was PCM"))
        self.assertNotIn("client adapter", reason)

    def test_a_client_stream_that_cannot_be_read_is_not_judged(self) -> None:
        self.assertIsNone(
            self._violation(
                _server_summary(declared=[PCM_24], stream=PCM_24),
                _client_summary(stream={"codec": "pcm"}),
            )
        )

    def _violation(self, server: dict[str, Any], client: dict[str, Any]) -> str | None:
        return stream_bit_depth_violation(server, client, bit_depth=24)


class Pcm24BitScenarioTests(unittest.TestCase):
    """The scenario applies the rule, and no other scenario does."""

    def test_matching_hashes_on_a_16_bit_stream_fail_the_case(self) -> None:
        matches, reason = _compare_summaries(
            require_scenario("server-initiated-pcm-24bit"),
            _server_summary(declared=[PCM_16], stream=PCM_16),
            _client_summary(),
        )
        self.assertFalse(matches)
        self.assertIn("cannot test 24-bit audio", reason)

    def test_matching_hashes_on_a_24_bit_stream_pass_the_case(self) -> None:
        matches, reason = _compare_summaries(
            require_scenario("server-initiated-pcm-24bit"),
            _server_summary(declared=[PCM_24], stream=PCM_24),
            _client_summary(stream=PCM_24),
        )
        self.assertTrue(matches, reason)
        self.assertEqual(reason, "PCM hashes match exactly")

    def test_a_hash_mismatch_keeps_its_own_reason(self) -> None:
        client = _client_summary()
        client["audio"]["received_pcm_sha256"] = "b" * 64
        matches, reason = _compare_summaries(
            require_scenario("server-initiated-pcm-24bit"),
            _server_summary(declared=[PCM_16], stream=PCM_16),
            client,
        )
        self.assertFalse(matches)
        self.assertTrue(reason.startswith("PCM hash mismatch"))

    def test_only_the_24_bit_scenario_names_a_bit_depth(self) -> None:
        naming = {
            scenario.id: scenario.verifies_stream_bit_depth
            for scenario in SCENARIOS.values()
            if scenario.verifies_stream_bit_depth is not None
        }
        self.assertEqual(naming, {"server-initiated-pcm-24bit": 24})

    def test_a_16_bit_stream_still_passes_the_plain_pcm_scenario(self) -> None:
        matches, reason = _compare_summaries(
            require_scenario("server-initiated-pcm"),
            _server_summary(declared=[PCM_16], stream=PCM_16),
            _client_summary(stream=PCM_16),
        )
        self.assertTrue(matches, reason)


if __name__ == "__main__":
    unittest.main()
