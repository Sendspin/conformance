"""Coverage for the PCM comparison the matrix applies to an audio-pcm case.

A player is handed a stream and is expected to render all of it, so only an
exact canonical hash match passes. Reproducing a truncated or corrupted stream
over the wire needs a misbehaving adapter, so the rule is exercised here against
synthetic summaries shaped like the ones the adapters really write.
"""

from __future__ import annotations

import unittest
from typing import Any

from conformance.runner import _compare_summaries
from conformance.scenarios import SCENARIOS, require_scenario

SOURCE_HASH = "a" * 64
OTHER_HASH = "b" * 64
FRAME_COUNT = 1000
CHANNELS = 2
SAMPLE_COUNT = FRAME_COUNT * CHANNELS


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


def _server_summary(**overrides: Any) -> dict[str, Any]:
    audio = {
        "source_pcm_sha256": SOURCE_HASH,
        "channels": CHANNELS,
        "frame_count": FRAME_COUNT,
    }
    return {
        "status": "ok",
        "implementation": "synthetic-server",
        "role": "server",
        "group_updates": [GROUP_UPDATE],
        "time_exchange": [],
        "availability_trace": AVAILABILITY_TRACE,
        "audio": {**audio, **overrides},
    }


def _client_summary(**overrides: Any) -> dict[str, Any]:
    audio = {
        "received_pcm_sha256": SOURCE_HASH,
        "received_sample_count": SAMPLE_COUNT,
        "audio_chunk_count": 10,
    }
    return {
        "status": "ok",
        "implementation": "synthetic-client",
        "role": "client",
        "audio": {**audio, **overrides},
    }


class PcmComparisonTests(unittest.TestCase):
    """An audio-pcm case passes on an exact hash match and on nothing else."""

    def test_exact_match_passes(self) -> None:
        matches, reason = self._compare(_server_summary(), _client_summary())
        self.assertTrue(matches, reason)
        self.assertEqual(reason, "PCM hashes match exactly")

    def test_truncated_stream_fails_and_reports_the_shortfall(self) -> None:
        """Receiving only a leading portion of the clip is not conformance."""
        matches, reason = self._compare(
            _server_summary(),
            _client_summary(
                received_pcm_sha256=OTHER_HASH,
                received_sample_count=SAMPLE_COUNT - 200,
            ),
        )
        self.assertFalse(matches)
        self.assertIn(f"client received {SAMPLE_COUNT - 200} of {SAMPLE_COUNT}", reason)
        self.assertIn("(200 fewer)", reason)

    def test_surplus_samples_fail_and_report_the_excess(self) -> None:
        matches, reason = self._compare(
            _server_summary(),
            _client_summary(
                received_pcm_sha256=OTHER_HASH,
                received_sample_count=SAMPLE_COUNT + 50,
            ),
        )
        self.assertFalse(matches)
        self.assertIn("(50 more)", reason)

    def test_equal_length_mismatch_reads_as_a_content_difference(self) -> None:
        matches, reason = self._compare(
            _server_summary(),
            _client_summary(received_pcm_sha256=OTHER_HASH),
        )
        self.assertFalse(matches)
        self.assertIn(f"sample counts equal ({SAMPLE_COUNT}), content differs", reason)

    def test_mismatch_always_reports_both_hashes(self) -> None:
        matches, reason = self._compare(
            _server_summary(),
            _client_summary(received_pcm_sha256=OTHER_HASH),
        )
        self.assertFalse(matches)
        self.assertIn(f"server={SOURCE_HASH} client={OTHER_HASH}", reason)

    def test_unreported_count_still_reports_the_mismatch(self) -> None:
        """The counts only describe a failure; their absence must not replace it."""
        server = _server_summary()
        del server["audio"]["frame_count"]
        matches, reason = self._compare(server, _client_summary(received_pcm_sha256=OTHER_HASH))
        self.assertFalse(matches)
        self.assertEqual(
            reason,
            f"PCM hash mismatch: server={SOURCE_HASH} client={OTHER_HASH}",
        )

    def test_every_audio_pcm_scenario_applies_the_exact_match(self) -> None:
        truncated = _client_summary(
            received_pcm_sha256=OTHER_HASH,
            received_sample_count=SAMPLE_COUNT - 200,
        )
        for scenario in SCENARIOS.values():
            if scenario.verification_mode != "audio-pcm":
                continue
            with self.subTest(scenario=scenario.id):
                matches, _ = _compare_summaries(scenario, _server_summary(), truncated)
                self.assertFalse(matches)

    def _compare(
        self,
        server_summary: dict[str, Any],
        client_summary: dict[str, Any],
    ) -> tuple[bool, str]:
        return _compare_summaries(
            require_scenario("server-initiated-pcm"),
            server_summary,
            client_summary,
        )


if __name__ == "__main__":
    unittest.main()
