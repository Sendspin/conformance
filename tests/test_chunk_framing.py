"""Coverage for the audio chunk framing rule behind `server-initiated-audio-chunk-framing`.

`roles/player/v1.md` fixes the player audio chunk at a 13-byte header and bounds
its duration at 150 ms, with 15 ms as the floor a server should keep to. Half of
the tracked implementations still frame a chunk with the 9-byte header that has
no `send_ahead`, and none of them sends an over-long chunk, so most of the ways
a case can fail here need a peer built to misbehave. They are exercised against
synthetic summaries instead.
"""

from __future__ import annotations

import unittest
from typing import Any

from conformance.chunk_framing import (
    HARNESS_GAP,
    chunk_framing_verdict,
    header_width,
    transported_frames,
)
from conformance.runner import _compare_summaries
from conformance.scenarios import SCENARIOS, require_scenario

SCENARIO_ID = "server-initiated-audio-chunk-framing"

# 8 kHz mono 16-bit, so one millisecond of audio is 16 bytes.
STREAM = {"codec": "pcm", "sample_rate": 8000, "bit_depth": 16, "channels": 1}
BYTES_PER_MS = 16


def _frame(duration_ms: int, *, header_bytes: int = 13, message_type: int = 4) -> dict[str, Any]:
    """Return the record of a frame carrying `duration_ms` of audio behind its header."""
    header = bytes([message_type]) + (1_000_000).to_bytes(8, "big") + (250_000).to_bytes(4, "big")
    frame = header[:header_bytes] + bytes(duration_ms * BYTES_PER_MS)
    return {"byte_count": len(frame), "leading_hex": frame[:13].hex()}


def _server_summary(
    durations_ms: list[int],
    *,
    header_bytes: int = 13,
    chunked_by: str | None = "implementation",
    **audio: Any,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "status": "ok",
        "implementation": "synthetic-server",
        "role": "server",
        "stream": STREAM,
        "audio": {
            "sent_audio_chunk_count": len(durations_ms),
            "sent_encoded_byte_count": sum(durations_ms) * BYTES_PER_MS,
            "sent_chunk_frames": [
                _frame(duration, header_bytes=header_bytes) for duration in durations_ms
            ],
            "chunked_by": chunked_by,
            **audio,
        },
    }
    return summary


def _client_summary(
    durations_ms: list[int],
    *,
    read_header_bytes: int = 13,
    observes_frames: bool = True,
    **audio: Any,
) -> dict[str, Any]:
    """Return a client that read `read_header_bytes` off each 13-byte-header frame."""
    return {
        "status": "ok",
        "implementation": "synthetic-client",
        "role": "client",
        "stream": STREAM,
        "audio": {
            "audio_chunk_count": len(durations_ms),
            "received_encoded_byte_count": (
                sum(durations_ms) * BYTES_PER_MS + (13 - read_header_bytes) * len(durations_ms)
            ),
            "received_chunk_frames": (
                [_frame(duration) for duration in durations_ms] if observes_frames else None
            ),
            **audio,
        },
    }


def _verdict(server: dict[str, Any], client: dict[str, Any]) -> tuple[bool, str]:
    return chunk_framing_verdict(server, client)


class ServerFramingTest(unittest.TestCase):
    """What the server put on the wire, judged from its own transported frames."""

    def test_passes_on_the_thirteen_byte_header(self) -> None:
        durations = [25, 25, 25]
        passed, reason = _verdict(_server_summary(durations), _client_summary(durations))
        self.assertTrue(passed, reason)
        self.assertIn("13-byte header", reason)
        self.assertIn("25.0-25.0 ms", reason)
        self.assertIn("saturation not judged", reason)

    def test_fails_a_nine_byte_header(self) -> None:
        durations = [25, 25]
        passed, reason = _verdict(
            _server_summary(durations, header_bytes=9), _client_summary(durations)
        )
        self.assertFalse(passed)
        self.assertIn("Server sent a 9-byte audio chunk header", reason)
        self.assertNotIn(HARNESS_GAP, reason)

    def test_a_server_that_kept_the_old_header_on_purpose_says_why(self) -> None:
        durations = [25, 25]
        passed, reason = _verdict(
            _server_summary(
                durations,
                header_bytes=9,
                legacy_header_reason="the client/hello was older than send_ahead",
            ),
            _client_summary(durations),
        )
        self.assertFalse(passed)
        self.assertIn("Server sent a 9-byte audio chunk header", reason)
        self.assertTrue(
            reason.endswith(
                "server adapter reported: the client/hello was older than send_ahead"
            ),
            reason,
        )

    def test_fails_a_frame_of_another_message_type(self) -> None:
        durations = [25, 25]
        server = _server_summary(durations)
        server["audio"]["sent_chunk_frames"][1] = _frame(25, message_type=8)
        passed, reason = _verdict(server, _client_summary(durations))
        self.assertFalse(passed)
        self.assertIn("binary message type 8 as frame 2 of 2", reason)

    def test_unobserved_server_frames_are_a_harness_gap(self) -> None:
        durations = [25, 25]
        passed, reason = _verdict(
            _server_summary(durations, sent_chunk_frames=None), _client_summary(durations)
        )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith(HARNESS_GAP), reason)
        self.assertIn("server adapter", reason)

    def test_a_server_that_sent_nothing_fails_as_itself(self) -> None:
        passed, reason = _verdict(_server_summary([]), _client_summary([]))
        self.assertFalse(passed)
        self.assertEqual(reason, "Server sent no audio chunks")

    def test_counts_that_fit_no_whole_header_are_a_harness_gap(self) -> None:
        durations = [25, 25, 25]
        for audio in (
            {"sent_audio_chunk_count": 2},
            {"sent_encoded_byte_count": None},
            {"sent_encoded_byte_count": 25 * 3 * BYTES_PER_MS + 1},
        ):
            with self.subTest(audio=audio):
                passed, reason = _verdict(
                    _server_summary(durations, **audio), _client_summary(durations)
                )
                self.assertFalse(passed)
                self.assertTrue(reason.startswith(HARNESS_GAP), reason)
                self.assertIn("header", reason)

    def test_an_uncounted_payload_is_named_as_such(self) -> None:
        durations = [25, 25]
        passed, reason = _verdict(
            _server_summary(durations, sent_encoded_byte_count=None), _client_summary(durations)
        )
        self.assertFalse(passed)
        self.assertIn("server adapter did not count the audio payload apart from", reason)

    def test_a_summary_without_an_audio_section_fails(self) -> None:
        durations = [25]
        server, client = _server_summary(durations), _client_summary(durations)
        del client["audio"]
        self.assertEqual(
            _verdict(server, client), (False, "Client summary is missing an 'audio' section")
        )
        del server["audio"]
        self.assertEqual(
            _verdict(server, client), (False, "Server summary is missing an 'audio' section")
        )


class ChunkDurationTest(unittest.TestCase):
    """The 150 ms ceiling and the 15 ms floor on what a server sends."""

    def test_fails_a_chunk_longer_than_the_ceiling(self) -> None:
        durations = [100, 151, 100]
        passed, reason = _verdict(_server_summary(durations), _client_summary(durations))
        self.assertFalse(passed)
        self.assertIn("audio chunk 2 of 3 lasting 151.0 ms", reason)
        self.assertIn("MUST NOT", reason)

    def test_passes_a_chunk_at_either_bound(self) -> None:
        durations = [150, 15, 150]
        passed, reason = _verdict(_server_summary(durations), _client_summary(durations))
        self.assertTrue(passed, reason)

    def test_fails_a_short_chunk_that_does_not_end_the_stream(self) -> None:
        durations = [25, 14, 25]
        passed, reason = _verdict(_server_summary(durations), _client_summary(durations))
        self.assertFalse(passed)
        self.assertIn("audio chunk 2 of 3 lasting 14.0 ms", reason)
        self.assertIn("SHOULD NOT", reason)

    def test_excuses_a_short_final_chunk(self) -> None:
        durations = [25, 25, 3]
        passed, reason = _verdict(_server_summary(durations), _client_summary(durations))
        self.assertTrue(passed, reason)

    def test_does_not_judge_chunks_the_adapter_cut(self) -> None:
        durations = [400, 2, 400]
        passed, reason = _verdict(
            _server_summary(durations, chunked_by="adapter"), _client_summary(durations)
        )
        self.assertTrue(passed, reason)
        self.assertIn("chunk durations not judged", reason)

    def test_unattributed_chunking_is_a_harness_gap(self) -> None:
        durations = [25, 25]
        passed, reason = _verdict(
            _server_summary(durations, chunked_by=None), _client_summary(durations)
        )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith(HARNESS_GAP), reason)

    def test_fails_a_stream_whose_durations_cannot_be_read(self) -> None:
        durations = [25, 25]
        server = _server_summary(durations)
        server["stream"] = {**STREAM, "codec": "flac"}
        passed, reason = _verdict(server, _client_summary(durations))
        self.assertFalse(passed)
        self.assertIn("Server negotiated flac where the client listed only PCM", reason)
        self.assertNotIn(HARNESS_GAP, reason)

    def test_an_unreported_stream_format_is_a_harness_gap(self) -> None:
        durations = [25, 25]
        server = _server_summary(durations)
        server["stream"] = None
        passed, reason = _verdict(server, _client_summary(durations))
        self.assertFalse(passed)
        self.assertTrue(reason.startswith(HARNESS_GAP), reason)


class ClientFramingTest(unittest.TestCase):
    """What the client made of the frames, and what it could show of them."""

    def test_fails_a_client_that_read_a_nine_byte_header(self) -> None:
        durations = [25, 25]
        passed, reason = _verdict(
            _server_summary(durations), _client_summary(durations, read_header_bytes=9)
        )
        self.assertFalse(passed)
        self.assertIn("Client read a 9-byte header", reason)

    def test_a_client_without_raw_frames_is_a_harness_gap(self) -> None:
        durations = [25, 25]
        passed, reason = _verdict(
            _server_summary(durations), _client_summary(durations, observes_frames=False)
        )
        self.assertFalse(passed)
        self.assertTrue(reason.startswith(HARNESS_GAP), reason)
        self.assertIn("client adapter", reason)
        self.assertIn("the client delivered exactly the audio behind them", reason)

    def test_a_client_without_raw_frames_is_still_failed_for_a_misread(self) -> None:
        durations = [25, 25]
        passed, reason = _verdict(
            _server_summary(durations),
            _client_summary(durations, read_header_bytes=9, observes_frames=False),
        )
        self.assertFalse(passed)
        self.assertIn("Client read a 9-byte header", reason)
        self.assertNotIn(HARNESS_GAP, reason)

    def test_a_client_reporting_no_payload_count_is_a_harness_gap(self) -> None:
        durations = [25, 25]
        for observes_frames in (True, False):
            with self.subTest(observes_frames=observes_frames):
                passed, reason = _verdict(
                    _server_summary(durations),
                    _client_summary(
                        durations,
                        observes_frames=observes_frames,
                        received_encoded_byte_count=None,
                    ),
                )
                self.assertFalse(passed)
                self.assertTrue(reason.startswith(HARNESS_GAP), reason)

    def test_fails_when_the_client_received_other_frames(self) -> None:
        passed, reason = _verdict(_server_summary([25, 25, 25]), _client_summary([25, 25]))
        self.assertFalse(passed)
        self.assertEqual(
            reason, "Client received 2 of the 3 audio chunk frames the server sent"
        )

    def test_fails_a_client_that_received_no_frames(self) -> None:
        passed, reason = _verdict(_server_summary([25, 25]), _client_summary([]))
        self.assertFalse(passed)
        self.assertEqual(
            reason, "Client received 0 of the 2 audio chunk frames the server sent"
        )

    def test_adapters_disagreeing_about_a_frame_is_a_harness_gap(self) -> None:
        durations = [25, 25]
        client = _client_summary(durations)
        client["audio"]["received_chunk_frames"][1] = _frame(25, message_type=8)
        passed, reason = _verdict(_server_summary(durations), client)
        self.assertFalse(passed)
        self.assertTrue(reason.startswith(HARNESS_GAP), reason)
        self.assertIn("frame 2 of 2", reason)

    def test_a_server_fault_leads_a_client_harness_gap(self) -> None:
        durations = [25, 25]
        passed, reason = _verdict(
            _server_summary(durations, header_bytes=9),
            _client_summary(durations, observes_frames=False),
        )
        self.assertFalse(passed)
        self.assertIn("Server sent a 9-byte audio chunk header", reason)


class FrameEvidenceTest(unittest.TestCase):
    """Reading the frame records and the width they imply."""

    def test_rejects_records_that_are_not_frames(self) -> None:
        for value in (
            None,
            "frames",
            [None],
            [{"byte_count": 413}],
            [{"byte_count": True, "leading_hex": "04"}],
            [{"byte_count": 413, "leading_hex": "zz"}],
            # A prefix shorter than the frame allows was truncated by the adapter.
            [{"byte_count": 413, "leading_hex": "04"}],
            [{"byte_count": 0, "leading_hex": ""}],
        ):
            with self.subTest(value=value):
                self.assertIsNone(transported_frames(value))

    def test_reads_a_frame_shorter_than_the_header(self) -> None:
        self.assertEqual(
            transported_frames([{"byte_count": 2, "leading_hex": "04ff"}]), [(2, b"\x04\xff")]
        )

    def test_width_is_the_bytes_beyond_the_payload(self) -> None:
        frames = transported_frames([_frame(25), _frame(25)])
        assert frames is not None
        self.assertEqual(header_width(frames, payload_byte_count=800, chunk_count=2), 13)
        self.assertEqual(header_width(frames, payload_byte_count=808, chunk_count=2), 9)
        self.assertIsNone(header_width(frames, payload_byte_count=801, chunk_count=2))
        self.assertIsNone(header_width(frames, payload_byte_count=800, chunk_count=3))
        self.assertIsNone(header_width(frames, payload_byte_count=900, chunk_count=2))
        self.assertIsNone(header_width([], payload_byte_count=0, chunk_count=0))


class ScenarioTest(unittest.TestCase):
    """The scenario is its own baseline key and its rule reaches no other scenario."""

    def test_scenario_starts_at_revision_one(self) -> None:
        scenario = require_scenario(SCENARIO_ID)
        self.assertEqual(scenario.scenario_revision, 1)
        self.assertEqual(scenario.verification_mode, "audio-chunk-framing")
        self.assertEqual(scenario.preferred_codec, "pcm")

    def test_no_other_scenario_is_judged_by_this_rule(self) -> None:
        judged = [
            scenario.id
            for scenario in SCENARIOS.values()
            if scenario.verification_mode == "audio-chunk-framing"
        ]
        self.assertEqual(judged, [SCENARIO_ID])

    def test_the_matrix_applies_the_rule_to_the_scenario(self) -> None:
        scenario = require_scenario(SCENARIO_ID)
        durations = [25, 25]
        passed, reason = _compare_summaries(
            scenario, _server_summary(durations), _client_summary(durations)
        )
        self.assertTrue(passed, reason)
        passed, reason = _compare_summaries(
            scenario, _server_summary(durations, header_bytes=9), _client_summary(durations)
        )
        self.assertFalse(passed)
        self.assertIn("9-byte", reason)

    def test_an_adapter_that_reported_its_own_failure_keeps_its_reason(self) -> None:
        scenario = require_scenario(SCENARIO_ID)
        client = {"status": "error", "reason": "no public hook for transported audio chunks"}
        passed, reason = _compare_summaries(scenario, _server_summary([25]), client)
        self.assertFalse(passed)
        self.assertIn("Client adapter reported: no public hook", reason)


if __name__ == "__main__":
    unittest.main()
