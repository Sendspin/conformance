"""Coverage for judging the `client/state` format-preference scenarios.

The matrix only reaches these rules with a client that sends the preference and
a server that records it. The cases that matter most — a client that asks some
other way, and a server that never acts on what it received — need summaries no
checked-in adapter pair produces.
"""

from __future__ import annotations

import unittest
from typing import Any

from conformance.implementations import IMPLEMENTATIONS, implementations_for_scenario
from conformance.runner import _compare_summaries
from conformance.scenarios import require_scenario

PCM_16 = {"codec": "pcm", "sample_rate": 8000, "bit_depth": 16, "channels": 1}
PCM_24 = {"codec": "pcm", "sample_rate": 8000, "bit_depth": 24, "channels": 1}
FLAC_16 = {"codec": "flac", "sample_rate": 8000, "bit_depth": 16, "channels": 1}

SCENARIO_IDS = ("client-initiated-state-format-pcm", "client-initiated-state-format-flac")


# Every server summary carries one, and a case whose summary lacks it never passes.
GROUP_UPDATE = {
    "type": "group/update",
    "payload": {"playback_state": "stopped", "group_id": "group-1", "group_name": "Kitchen"},
}


def _server_summary(
    *,
    received: dict[str, Any] | None,
    stream: dict[str, Any] | None = PCM_16,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "implementation": "synthetic-server",
        "role": "server",
        "group_updates": [GROUP_UPDATE],
        "stream": stream,
        "format_preference": {"received": received},
    }


def _client_summary(
    *,
    requested: dict[str, Any] = PCM_16,
    final: dict[str, Any] | None = PCM_16,
    stream_start_count: int = 2,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "implementation": "synthetic-client",
        "role": "client",
        "renegotiation": {
            "requested": requested,
            "stream_start_count": stream_start_count,
            "initial_format": PCM_24,
            "final_format": final,
        },
    }


class FormatPreferenceComparisonTests(unittest.TestCase):
    """The verdict rests on what the server saw in `client/state`."""

    def setUp(self) -> None:
        self.scenario = require_scenario("client-initiated-state-format-pcm")

    def test_passes_when_the_server_received_and_applied_the_preference(self) -> None:
        matches, reason = _compare_summaries(
            self.scenario, _server_summary(received=PCM_16), _client_summary()
        )
        self.assertTrue(matches, reason)

    def test_a_format_change_without_a_client_state_preference_fails(self) -> None:
        """A client that got its new format some other way did not use `client/state`."""
        matches, reason = _compare_summaries(
            self.scenario, _server_summary(received=None), _client_summary()
        )
        self.assertFalse(matches)
        self.assertIn("client/state", reason)
        self.assertIn("roles/player/v1.md", reason)

    def test_a_server_adapter_that_records_no_evidence_is_a_harness_gap(self) -> None:
        """An adapter that cannot say what it received is not evidence against a client."""
        server = _server_summary(received=PCM_16)
        del server["format_preference"]
        matches, reason = _compare_summaries(self.scenario, server, _client_summary())
        self.assertFalse(matches)
        self.assertIn("Harness gap", reason)

    def test_a_preference_for_another_format_than_the_scenario_target_fails(self) -> None:
        """Both sides agreeing on the reverse transition is not this scenario."""
        matches, reason = _compare_summaries(
            self.scenario,
            _server_summary(received=PCM_24, stream=PCM_24),
            _client_summary(requested=PCM_24, final=PCM_24),
        )
        self.assertFalse(matches)
        self.assertIn("24bit", reason)
        self.assertIn("this scenario", reason)

    def test_the_flac_scenario_rejects_a_preference_for_pcm(self) -> None:
        matches, reason = _compare_summaries(
            require_scenario("client-initiated-state-format-flac"),
            _server_summary(received=PCM_16),
            _client_summary(),
        )
        self.assertFalse(matches)
        self.assertIn("this scenario", reason)

    def test_the_flac_scenario_passes_a_preference_for_flac(self) -> None:
        matches, reason = _compare_summaries(
            require_scenario("client-initiated-state-format-flac"),
            _server_summary(received=FLAC_16, stream=FLAC_16),
            _client_summary(requested=FLAC_16, final=FLAC_16),
        )
        self.assertTrue(matches, reason)

    def test_a_preference_the_client_did_not_report_fails(self) -> None:
        matches, reason = _compare_summaries(
            self.scenario,
            _server_summary(received=PCM_16),
            _client_summary(requested=FLAC_16, final=FLAC_16),
        )
        self.assertFalse(matches)
        self.assertIn("client reports preferring", reason)

    def test_a_client_claim_the_server_stream_contradicts_fails(self) -> None:
        """The client's word that the format changed does not outweigh the server's."""
        matches, reason = _compare_summaries(
            self.scenario,
            _server_summary(received=PCM_16, stream=PCM_24),
            _client_summary(),
        )
        self.assertFalse(matches)
        self.assertIn("server's last stream/start was pcm/8000Hz/24bit/1ch", reason)

    def test_a_received_preference_the_server_never_applied_fails(self) -> None:
        matches, reason = _compare_summaries(
            self.scenario,
            _server_summary(received=PCM_16, stream=PCM_24),
            _client_summary(final=None, stream_start_count=1),
        )
        self.assertFalse(matches)
        self.assertIn("Server kept the stream format", reason)
        self.assertIn("SHOULD", reason)

    def test_a_change_the_server_sent_but_the_client_missed_blames_the_client(self) -> None:
        matches, reason = _compare_summaries(
            self.scenario,
            _server_summary(received=PCM_16, stream=PCM_16),
            _client_summary(final=None, stream_start_count=1),
        )
        self.assertFalse(matches)
        self.assertIn("client did not observe", reason)

    def test_a_client_summary_without_a_renegotiation_block_fails(self) -> None:
        client = _client_summary()
        del client["renegotiation"]
        matches, reason = _compare_summaries(
            self.scenario, _server_summary(received=PCM_16), client
        )
        self.assertFalse(matches)
        self.assertIn("renegotiation block", reason)

    def test_a_new_format_other_than_the_preferred_one_fails(self) -> None:
        matches, reason = _compare_summaries(
            self.scenario,
            _server_summary(received=PCM_16, stream=FLAC_16),
            _client_summary(final=FLAC_16),
        )
        self.assertFalse(matches)
        self.assertIn("does not match", reason)


class FormatPreferenceMatrixTests(unittest.TestCase):
    """The scenarios carry no capability flag of their own to opt out with."""

    def test_only_the_generic_initiator_role_and_codec_traits_gate_a_client(self) -> None:
        for scenario_id in SCENARIO_IDS:
            scenario = require_scenario(scenario_id)
            expected = [
                name
                for name, spec in sorted(IMPLEMENTATIONS.items())
                if spec.client.supports_client_initiated
                and "player" in spec.client.supported_role_families
                and spec.client.supports_codec(scenario.preferred_codec)
            ]
            with self.subTest(scenario=scenario_id):
                self.assertEqual(
                    implementations_for_scenario(role="client", scenario=scenario),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
