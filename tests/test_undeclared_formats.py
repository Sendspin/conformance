"""Coverage for the spec MUST that a negotiated format be one the client declared.

The pairing that violates it in the published matrix (a sendspin-go server
negotiating 24-bit FLAC to a sendspin-jvm client that offered only 16-bit) needs
a Go toolchain and a JDK to reproduce, so the rule is exercised here against
synthetic summaries shaped like the ones the adapters really write.
"""

from __future__ import annotations

import unittest
from typing import Any

from conformance.declared_formats import undeclared_format_violation
from conformance.runner import _compare_summaries
from conformance.scenarios import require_scenario

FLAC_16 = {"codec": "flac", "sample_rate": 8000, "bit_depth": 16, "channels": 1}
FLAC_24 = {"codec": "flac", "sample_rate": 8000, "bit_depth": 24, "channels": 1}
OPUS_16 = {"codec": "opus", "sample_rate": 48000, "bit_depth": 16, "channels": 1}
PCM_16 = {"codec": "pcm", "sample_rate": 8000, "bit_depth": 16, "channels": 1}
PCM_24 = {"codec": "pcm", "sample_rate": 8000, "bit_depth": 24, "channels": 1}

ENCODED_SHA = "b" * 64


def _server_summary(
    *,
    declared: list[Any] | None,
    stream: dict[str, Any] | None,
    include_player_support: bool = True,
) -> dict[str, Any]:
    """Build a server summary that passes encoded-bytes verification on its own."""
    payload: dict[str, Any] = {"name": "synthetic-client"}
    if include_player_support:
        payload["player@v1_support"] = {"supported_formats": declared}
    return {
        "status": "ok",
        "implementation": "synthetic-server",
        "role": "server",
        "peer_hello": {"type": "client/hello", "payload": payload},
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


def _renegotiation_client_summary(
    *,
    initial: dict[str, Any],
    final: dict[str, Any],
) -> dict[str, Any]:
    """Build a client summary that passes renegotiation verification on its own."""
    return {
        "status": "ok",
        "implementation": "synthetic-client",
        "role": "client",
        "renegotiation": {
            "requested": final,
            "stream_start_count": 2,
            "initial_format": initial,
            "final_format": final,
        },
    }


class UndeclaredFormatViolationTest(unittest.TestCase):
    """The rule itself, independent of any scenario's own verification."""

    def test_undeclared_negotiated_format_is_reported(self) -> None:
        violation = undeclared_format_violation(
            _server_summary(declared=[FLAC_16], stream=FLAC_24),
            _client_summary(stream=FLAC_24),
        )
        self.assertIsNotNone(violation)
        assert violation is not None
        # The reason has to name what was negotiated, where it was seen, and
        # what the client offered instead.
        self.assertIn("24-bit", violation)
        self.assertIn("16-bit", violation)
        self.assertIn("server.stream", violation)
        self.assertIn("client.stream", violation)

    def test_declared_negotiated_format_is_not_reported(self) -> None:
        self.assertIsNone(
            undeclared_format_violation(
                _server_summary(declared=[FLAC_16], stream=FLAC_16),
                _client_summary(stream=FLAC_16),
            )
        )

    def test_opus_bit_depth_is_ignored(self) -> None:
        """The spec ignores `bit_depth` for opus, so differing on it is no violation."""
        self.assertIsNone(
            undeclared_format_violation(
                _server_summary(declared=[OPUS_16], stream={**OPUS_16, "bit_depth": 0}),
                _client_summary(stream={**OPUS_16, "bit_depth": 0}),
            )
        )

    def test_opus_still_matches_on_the_fields_that_count(self) -> None:
        """Ignoring `bit_depth` must not make every opus format interchangeable."""
        self.assertIsNotNone(
            undeclared_format_violation(
                _server_summary(
                    declared=[OPUS_16], stream={**OPUS_16, "sample_rate": 24000}
                ),
                _client_summary(stream={**OPUS_16, "sample_rate": 24000}),
            )
        )

    def test_codec_case_does_not_create_a_violation(self) -> None:
        """Producers disagree on codec casing; the shared helpers casefold it."""
        self.assertIsNone(
            undeclared_format_violation(
                _server_summary(declared=[{**FLAC_16, "codec": "FLAC"}], stream=FLAC_16),
                _client_summary(stream=FLAC_16),
            )
        )

    def test_absent_peer_hello_is_skipped(self) -> None:
        summary = _server_summary(declared=[FLAC_16], stream=FLAC_24)
        del summary["peer_hello"]
        self.assertIsNone(
            undeclared_format_violation(summary, _client_summary(stream=FLAC_24))
        )

    def test_absent_player_support_is_skipped(self) -> None:
        self.assertIsNone(
            undeclared_format_violation(
                _server_summary(declared=None, stream=FLAC_24, include_player_support=False),
                _client_summary(stream=FLAC_24),
            )
        )

    def test_empty_supported_formats_is_skipped(self) -> None:
        """An empty list is read as no declaration, not as declaring nothing."""
        self.assertIsNone(
            undeclared_format_violation(
                _server_summary(declared=[], stream=FLAC_24),
                _client_summary(stream=FLAC_24),
            )
        )

    def test_unparsable_supported_formats_is_skipped(self) -> None:
        self.assertIsNone(
            undeclared_format_violation(
                _server_summary(declared="flac", stream=FLAC_24),
                _client_summary(stream=FLAC_24),
            )
        )

    def test_one_unreadable_entry_suppresses_the_whole_case(self) -> None:
        """A format could be undeclared only because its entry failed to parse."""
        self.assertIsNone(
            undeclared_format_violation(
                _server_summary(declared=[FLAC_16, {"codec": "flac"}], stream=FLAC_24),
                _client_summary(stream=FLAC_24),
            )
        )

    def test_no_negotiated_stream_is_not_a_violation(self) -> None:
        """Metadata, controller and artwork scenarios negotiate no player stream."""
        self.assertIsNone(
            undeclared_format_violation(
                _server_summary(declared=[FLAC_16], stream=None),
                _client_summary(stream=None),
            )
        )


class CompareSummariesTest(unittest.TestCase):
    """The rule as the runner applies it, so the wiring is covered too."""

    def test_encoded_audio_case_fails_on_an_undeclared_format(self) -> None:
        scenario = require_scenario("server-initiated-flac")
        matches, reason = _compare_summaries(
            scenario,
            _server_summary(declared=[FLAC_16], stream=FLAC_24),
            _client_summary(stream=FLAC_24),
        )
        self.assertFalse(matches)
        self.assertIn("not declared by the client", reason)

    def test_encoded_audio_case_passes_when_the_format_was_declared(self) -> None:
        scenario = require_scenario("server-initiated-flac")
        matches, reason = _compare_summaries(
            scenario,
            _server_summary(declared=[FLAC_16], stream=FLAC_16),
            _client_summary(stream=FLAC_16),
        )
        self.assertTrue(matches, reason)

    def test_a_real_failure_keeps_its_own_reason(self) -> None:
        """The declaration check must not displace the more proximate failure."""
        server = _server_summary(declared=[FLAC_16], stream=FLAC_24)
        server["audio"]["sent_audio_chunk_count"] = 0
        matches, reason = _compare_summaries(
            require_scenario("server-initiated-flac"),
            server,
            _client_summary(stream=FLAC_24),
        )
        self.assertFalse(matches)
        self.assertIn("zero FLAC audio chunks sent", reason)

    def test_renegotiation_passes_when_both_formats_were_declared(self) -> None:
        scenario = require_scenario("client-initiated-request-format-pcm")
        matches, reason = _compare_summaries(
            scenario,
            _server_summary(declared=[PCM_24, PCM_16], stream=PCM_16),
            _renegotiation_client_summary(initial=PCM_16, final=PCM_24),
        )
        self.assertTrue(matches, reason)

    def test_renegotiation_fails_when_only_one_format_was_declared(self) -> None:
        scenario = require_scenario("client-initiated-request-format-pcm")
        matches, reason = _compare_summaries(
            scenario,
            _server_summary(declared=[PCM_16], stream=PCM_16),
            _renegotiation_client_summary(initial=PCM_16, final=PCM_24),
        )
        self.assertFalse(matches)
        self.assertIn("24-bit", reason)
        self.assertIn("client.renegotiation.final_format", reason)


if __name__ == "__main__":
    unittest.main()
