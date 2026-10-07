"""Coverage for the declared-priority rule behind `server-initiated-opus`.

`roles/player/v1.md` requires every player to list `flac` or `pcm`, and says a
server SHOULD select the highest-priority `supported_formats` entry it can
produce. The scenario therefore has the client list opus first with a mandatory
entry behind it, and passes only when the server streamed opus. An opus-only
list is a `client/hello` no conformant client sends, so it must not go green,
and reproducing a server that ignores the priority needs one built to do so;
both are exercised here against synthetic summaries.
"""

from __future__ import annotations

import unittest
from typing import Any

from conformance.declared_formats import format_priority_violation
from conformance.runner import _compare_summaries
from conformance.scenarios import SCENARIOS, require_scenario

OPUS = {"codec": "opus", "sample_rate": 8000, "bit_depth": 16, "channels": 1}
PCM = {"codec": "pcm", "sample_rate": 8000, "bit_depth": 16, "channels": 1}
FLAC = {"codec": "flac", "sample_rate": 8000, "bit_depth": 16, "channels": 1}

ENCODED_SHA = "b" * 64


def _server_summary(*, declared: Any, stream: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "status": "ok",
        "implementation": "synthetic-server",
        "role": "server",
        "peer_hello": {
            "type": "client/hello",
            "payload": {"player@v1_support": {"supported_formats": declared}},
        },
        "stream": stream,
        "audio": {"sent_audio_chunk_count": 4, "sent_encoded_sha256": ENCODED_SHA},
    }


def _client_summary(*, stream: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "status": "ok",
        "implementation": "synthetic-client",
        "role": "client",
        "stream": stream,
        "audio": {"audio_chunk_count": 4, "received_encoded_sha256": ENCODED_SHA},
    }


def _violation(*, declared: Any, stream: dict[str, Any] | None = OPUS) -> str | None:
    return format_priority_violation(
        _server_summary(declared=declared, stream=stream), preferred_codec="opus"
    )


class FormatPriorityViolationTest(unittest.TestCase):
    """The rule itself, independent of the scenario's own verification."""

    def test_preferred_codec_first_with_a_mandatory_entry_holds(self) -> None:
        for fallback in (PCM, FLAC):
            with self.subTest(fallback=fallback["codec"]):
                self.assertIsNone(_violation(declared=[OPUS, fallback]))

    def test_a_list_without_flac_or_pcm_is_reported(self) -> None:
        violation = _violation(declared=[OPUS])
        assert violation is not None
        self.assertIn("no flac or pcm entry", violation)
        self.assertIn("roles/player/v1.md", violation)

    def test_preferred_codec_listed_behind_the_fallback_is_reported(self) -> None:
        violation = _violation(declared=[PCM, OPUS])
        assert violation is not None
        self.assertIn("did not list OPUS first", violation)

    def test_selecting_the_fallback_is_reported_as_the_spec_should(self) -> None:
        violation = _violation(declared=[OPUS, PCM], stream=PCM)
        assert violation is not None
        self.assertIn("Server selected PCM", violation)
        self.assertIn("SHOULD", violation)

    def test_codec_case_does_not_create_a_violation(self) -> None:
        self.assertIsNone(
            _violation(declared=[{**OPUS, "codec": "OPUS"}, PCM], stream={**OPUS, "codec": "Opus"})
        )

    def test_a_missing_declaration_is_reported(self) -> None:
        """The declared list is the stimulus, so its absence is not a pass."""
        for declared in (None, []):
            with self.subTest(declared=declared):
                violation = _violation(declared=declared)
                assert violation is not None
                self.assertIn("records no supported_formats", violation)

    def test_an_unreadable_declaration_is_reported(self) -> None:
        for declared in ("opus", [OPUS, {"codec": "pcm"}]):
            with self.subTest(declared=declared):
                violation = _violation(declared=declared)
                assert violation is not None
                self.assertIn("cannot be read as an audio format", violation)

    def test_a_codec_the_client_never_listed_is_left_to_the_other_checks(self) -> None:
        self.assertIsNone(_violation(declared=[OPUS, PCM], stream=FLAC))

    def test_a_missing_stream_is_left_to_the_scenario_comparison(self) -> None:
        self.assertIsNone(_violation(declared=[OPUS, PCM], stream=None))


class OpusScenarioTest(unittest.TestCase):
    """The rule as `server-initiated-opus` applies it."""

    def _compare(self, *, declared: Any, stream: dict[str, Any] | None) -> tuple[bool, str]:
        return _compare_summaries(
            require_scenario("server-initiated-opus"),
            _server_summary(declared=declared, stream=stream),
            _client_summary(stream=stream),
        )

    def test_opus_selected_from_a_legal_list_passes(self) -> None:
        matches, reason = self._compare(declared=[OPUS, PCM], stream=OPUS)
        self.assertTrue(matches, reason)

    def test_an_opus_only_list_fails_even_when_opus_was_streamed(self) -> None:
        matches, reason = self._compare(declared=[OPUS], stream=OPUS)
        self.assertFalse(matches)
        self.assertIn("no flac or pcm entry", reason)

    def test_falling_back_to_pcm_fails(self) -> None:
        matches, reason = self._compare(declared=[OPUS, PCM], stream=PCM)
        self.assertFalse(matches)
        self.assertIn("Server selected PCM", reason)

    def test_an_unlisted_codec_fails_as_not_opus(self) -> None:
        matches, reason = self._compare(declared=[OPUS, PCM], stream=FLAC)
        self.assertFalse(matches)
        self.assertIn("Server did not negotiate OPUS transport", reason)

    def test_a_failed_server_keeps_its_own_status(self) -> None:
        """A server that never recorded the hello is not blamed for the missing list."""
        matches, reason = _compare_summaries(
            require_scenario("server-initiated-opus"),
            {"status": "timeout", "implementation": "synthetic-server", "role": "server"},
            _client_summary(stream=None),
        )
        self.assertFalse(matches)
        self.assertNotIn("supported_formats", reason)

    def test_only_the_opus_scenario_applies_the_rule(self) -> None:
        self.assertEqual(
            [scenario.id for scenario in SCENARIOS.values() if scenario.verifies_format_priority],
            ["server-initiated-opus"],
        )

    def test_the_flac_scenario_still_accepts_a_list_of_one(self) -> None:
        matches, reason = _compare_summaries(
            require_scenario("server-initiated-flac"),
            _server_summary(declared=[FLAC], stream=FLAC),
            _client_summary(stream=FLAC),
        )
        self.assertTrue(matches, reason)


if __name__ == "__main__":
    unittest.main()
