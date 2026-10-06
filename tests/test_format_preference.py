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

SCENARIO_IDS = ("client-initiated-state-format-pcm", "client-initiated-state-format-flac")


def _server_summary(*, received: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "status": "ok",
        "implementation": "synthetic-server",
        "role": "server",
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

    def test_a_server_summary_without_the_evidence_fails(self) -> None:
        server = _server_summary(received=PCM_16)
        del server["format_preference"]
        matches, reason = _compare_summaries(self.scenario, server, _client_summary())
        self.assertFalse(matches)
        self.assertIn("client/state", reason)

    def test_a_preference_the_client_did_not_report_fails(self) -> None:
        matches, reason = _compare_summaries(
            self.scenario, _server_summary(received=PCM_24), _client_summary()
        )
        self.assertFalse(matches)
        self.assertIn("24bit", reason)
        self.assertIn("16bit", reason)

    def test_a_received_preference_the_server_never_applied_fails(self) -> None:
        matches, reason = _compare_summaries(
            self.scenario,
            _server_summary(received=PCM_16),
            _client_summary(final=None, stream_start_count=1),
        )
        self.assertFalse(matches)
        self.assertIn("stream/start", reason)
        self.assertIn("Format preference", reason)


class FormatPreferenceMatrixTests(unittest.TestCase):
    """No implementation can opt out of the scenarios."""

    def test_every_client_that_can_dial_out_as_a_player_is_judged(self) -> None:
        for scenario_id in SCENARIO_IDS:
            scenario = require_scenario(scenario_id)
            expected = [
                name
                for name, spec in sorted(IMPLEMENTATIONS.items())
                if spec.client.supports_client_initiated
                and "player" in spec.client.supported_role_families
            ]
            with self.subTest(scenario=scenario_id):
                self.assertEqual(
                    implementations_for_scenario(role="client", scenario=scenario),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
