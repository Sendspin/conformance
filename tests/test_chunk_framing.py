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

from conformance.adapters._aiosendspin_protocol_evidence import (
    ChunkPayloadSizes,
    binary_frame_record,
)
from conformance.chunk_framing import HARNESS_GAP, Frame, chunk_framing_verdict, transported_frames
from conformance.runner import _compare_summaries
from conformance.scenarios import SCENARIOS, require_scenario

SCENARIO_ID = "server-initiated-audio-chunk-framing"

# 8 kHz mono 16-bit, so one millisecond of audio is 16 bytes.
STREAM = {"codec": "pcm", "sample_rate": 8000, "bit_depth": 16, "channels": 1}
BYTES_PER_MS = 16


# Every server summary carries one, and a case whose summary lacks it never passes.
GROUP_UPDATE = {
    "type": "group/update",
    "payload": {"playback_state": "stopped", "group_id": "group-1", "group_name": "Kitchen"},
}


def _frame(
    duration_ms: int,
    *,
    header_bytes: int = 13,
    message_type: int = 4,
    read_header_bytes: int | None = None,
    paired: bool = True,
) -> dict[str, Any]:
    """Return the record of a frame carrying `duration_ms` of audio behind its header.

    `read_header_bytes` is what the reporting side took the header to be, where
    that differs from what the frame was built with.
    """
    header = bytes([message_type]) + (1_000_000).to_bytes(8, "big") + (250_000).to_bytes(4, "big")
    frame = header[:header_bytes] + bytes(duration_ms * BYTES_PER_MS)
    read = header_bytes if read_header_bytes is None else read_header_bytes
    return {
        "byte_count": len(frame),
        "leading_hex": frame[:13].hex(),
        "payload_byte_count": len(frame) - read if paired else None,
    }


def _server_summary(
    durations_ms: list[int],
    *,
    header_bytes: int = 13,
    chunked_by: str | None = "implementation",
    **audio: Any,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "implementation": "synthetic-server",
        "role": "server",
        "group_updates": [GROUP_UPDATE],
        "stream": STREAM,
        "audio": {
            "sent_chunk_frames": [
                _frame(duration, header_bytes=header_bytes) for duration in durations_ms
            ],
            "chunked_by": chunked_by,
            **audio,
        },
    }


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
                [_frame(duration, read_header_bytes=read_header_bytes) for duration in durations_ms]
                if observes_frames
                else None
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
        self.assertIn("All 3 audio chunks carried the 13-byte header", reason)
        self.assertIn("25.0-25.0 ms", reason)
        self.assertIn("saturation not judged", reason)

    def test_fails_a_nine_byte_header(self) -> None:
        durations = [25, 25]
        passed, reason = _verdict(
            _server_summary(durations, header_bytes=9), _client_summary(durations)
        )
        self.assertFalse(passed)
        self.assertIn("Server sent a header of 9 bytes on 2 of 2 audio chunks", reason)
        self.assertNotIn(HARNESS_GAP, reason)

    def test_fails_headers_that_only_average_thirteen_bytes(self) -> None:
        durations = [25, 25, 25]
        server = _server_summary(durations)
        server["audio"]["sent_chunk_frames"][0] = _frame(25, header_bytes=9)
        server["audio"]["sent_chunk_frames"][1] = _frame(25, read_header_bytes=17)
        passed, reason = _verdict(server, _client_summary(durations))
        self.assertFalse(passed)
        self.assertIn("Server sent a header of 9, 17 bytes on 2 of 3 audio chunks", reason)

    def test_fails_a_single_frame_with_another_header(self) -> None:
        durations = [25, 25, 25]
        server = _server_summary(durations)
        server["audio"]["sent_chunk_frames"][2] = _frame(25, header_bytes=9)
        passed, reason = _verdict(server, _client_summary(durations))
        self.assertFalse(passed)
        self.assertIn("Server sent a header of 9 bytes on 1 of 3 audio chunks", reason)

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
        self.assertIn("Server sent a header of 9 bytes", reason)
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

    def test_a_frame_whose_audio_is_unknown_is_a_harness_gap(self) -> None:
        durations = [25, 25, 25]
        server = _server_summary(durations, legacy_header_reason="not the cause")
        server["audio"]["sent_chunk_frames"][1] = _frame(25, paired=False)
        passed, reason = _verdict(server, _client_summary(durations))
        self.assertFalse(passed)
        self.assertTrue(reason.startswith(HARNESS_GAP), reason)
        self.assertIn("server adapter could not tell which audio 1 of its 3 frames", reason)
        self.assertNotIn("not the cause", reason)

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

    def test_fails_a_stream_that_is_not_pcm(self) -> None:
        durations = [25, 25]
        server = _server_summary(durations)
        server["stream"] = {**STREAM, "codec": "flac"}
        passed, reason = _verdict(server, _client_summary(durations))
        self.assertFalse(passed)
        self.assertIn("Server negotiated flac where the client listed only PCM", reason)
        self.assertNotIn(HARNESS_GAP, reason)

    def test_an_unusable_stream_format_is_a_harness_gap(self) -> None:
        durations = [25, 25]
        for stream in (
            None,
            {**STREAM, "sample_rate": 0},
            {**STREAM, "channels": 0},
            {**STREAM, "bit_depth": 0},
            {**STREAM, "bit_depth": 4},
            {**STREAM, "sample_rate": -8000},
        ):
            with self.subTest(stream=stream):
                server = _server_summary(durations)
                server["stream"] = stream
                passed, reason = _verdict(server, _client_summary(durations))
                self.assertFalse(passed)
                self.assertTrue(reason.startswith(HARNESS_GAP), reason)
                self.assertIn("no usable stream format", reason)


class ClientFramingTest(unittest.TestCase):
    """What the client made of the frames, and what it could show of them."""

    def test_fails_a_client_that_read_a_nine_byte_header(self) -> None:
        durations = [25, 25]
        passed, reason = _verdict(
            _server_summary(durations), _client_summary(durations, read_header_bytes=9)
        )
        self.assertFalse(passed)
        self.assertIn("Client read a header of 9 bytes on 2 of 2 audio chunks", reason)

    def test_fails_a_client_whose_misreads_average_thirteen_bytes(self) -> None:
        durations = [25, 25]
        client = _client_summary(durations)
        client["audio"]["received_chunk_frames"] = [
            _frame(25, read_header_bytes=9),
            _frame(25, read_header_bytes=17),
        ]
        passed, reason = _verdict(_server_summary(durations), client)
        self.assertFalse(passed)
        self.assertIn("Client read a header of 9, 17 bytes on 2 of 2 audio chunks", reason)

    def test_a_client_frame_whose_audio_is_unknown_is_a_harness_gap(self) -> None:
        durations = [25, 25]
        client = _client_summary(durations)
        client["audio"]["received_chunk_frames"][0] = _frame(25, paired=False)
        passed, reason = _verdict(_server_summary(durations), client)
        self.assertFalse(passed)
        self.assertTrue(reason.startswith(HARNESS_GAP), reason)
        self.assertIn("client adapter could not tell which audio 1 of its 2 frames", reason)

    def test_a_client_without_raw_frames_is_a_harness_gap(self) -> None:
        durations = [25, 25]
        for audio in ({}, {"received_encoded_byte_count": None}):
            with self.subTest(audio=audio):
                passed, reason = _verdict(
                    _server_summary(durations),
                    _client_summary(durations, observes_frames=False, **audio),
                )
                self.assertFalse(passed)
                self.assertTrue(reason.startswith(HARNESS_GAP), reason)
                self.assertIn("client adapter", reason)

    def test_a_client_without_raw_frames_is_still_failed_for_a_misread(self) -> None:
        durations = [25, 25]
        passed, reason = _verdict(
            _server_summary(durations),
            _client_summary(durations, read_header_bytes=9, observes_frames=False),
        )
        self.assertFalse(passed)
        self.assertIn("Client delivered 808 bytes of audio from 2 chunks that carried 800", reason)
        self.assertNotIn(HARNESS_GAP, reason)

    def test_fails_when_the_client_received_fewer_frames(self) -> None:
        for received in ([25, 25], []):
            with self.subTest(received=received):
                passed, reason = _verdict(
                    _server_summary([25, 25, 25]), _client_summary(received)
                )
                self.assertFalse(passed)
                self.assertEqual(
                    reason,
                    f"Client received {len(received)} of the 3 audio chunk frames the server sent",
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
        self.assertIn("Server sent a header of 9 bytes", reason)


class FrameEvidenceTest(unittest.TestCase):
    """Reading the frame records, and pairing a frame with the audio it carried."""

    def test_rejects_records_that_are_not_frames(self) -> None:
        for value in (
            None,
            "frames",
            [None],
            [{"byte_count": 413}],
            [{"byte_count": True, "leading_hex": "04"}],
            [{"byte_count": -1, "leading_hex": "04"}],
            [{"byte_count": 413, "leading_hex": "zz"}],
            # A prefix shorter than the frame allows was truncated by the adapter.
            [{"byte_count": 413, "leading_hex": "04"}],
            [{"byte_count": 0, "leading_hex": ""}],
            # No frame carries more audio than it has bytes.
            [{"byte_count": 2, "leading_hex": "04ff", "payload_byte_count": 3}],
        ):
            with self.subTest(value=value):
                self.assertIsNone(transported_frames(value))

    def test_reads_a_frame_with_and_without_its_payload_size(self) -> None:
        self.assertEqual(
            transported_frames([{"byte_count": 2, "leading_hex": "04ff"}]),
            [Frame(2, b"\x04\xff", None)],
        )
        frames = transported_frames([_frame(25)])
        assert frames is not None
        self.assertEqual(frames[0].header_bytes, 13)

    def test_pairs_a_frame_with_the_chunk_whose_timestamp_it_carries(self) -> None:
        sizes = ChunkPayloadSizes()
        sizes.record(1_000_000, bytes(400))
        sizes.record(1_025_000, bytes(200))

        def frame(timestamp_us: int, payload: int) -> dict[str, Any]:
            header = b"\x04" + timestamp_us.to_bytes(8, "big", signed=True) + bytes(4)
            return binary_frame_record(header + bytes(payload))

        paired = sizes.paired([frame(1_025_000, 200), frame(1_000_000, 400), frame(7, 400)])
        self.assertEqual([record["payload_byte_count"] for record in paired], [200, 400, None])
        self.assertEqual(paired[0]["byte_count"], 213)

    def test_does_not_pair_chunks_sharing_a_timestamp_or_a_truncated_frame(self) -> None:
        sizes = ChunkPayloadSizes()
        sizes.record(5, bytes(400))
        sizes.record(5, bytes(200))
        sizes.record(0, bytes(8))
        frame = binary_frame_record(b"\x04" + (5).to_bytes(8, "big") + bytes(404))
        self.assertIsNone(sizes.paired([frame])[0]["payload_byte_count"])
        # Too short to hold a timestamp, so it must not read as timestamp zero.
        short = binary_frame_record(b"\x04\x00")
        self.assertIsNone(sizes.paired([short])[0]["payload_byte_count"])


class ScenarioTest(unittest.TestCase):
    """The scenario is its own baseline key and its rule reaches no other scenario."""

    def test_scenario_is_at_revision_two(self) -> None:
        scenario = require_scenario(SCENARIO_ID)
        self.assertEqual(scenario.scenario_revision, 2)
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
        self.assertIn("header of 9 bytes", reason)

    def test_an_adapter_that_reported_its_own_failure_keeps_its_reason(self) -> None:
        scenario = require_scenario(SCENARIO_ID)
        client = {"status": "error", "reason": "no public hook for transported audio chunks"}
        passed, reason = _compare_summaries(scenario, _server_summary([25]), client)
        self.assertFalse(passed)
        self.assertIn("Client adapter reported: no public hook", reason)


if __name__ == "__main__":
    unittest.main()
