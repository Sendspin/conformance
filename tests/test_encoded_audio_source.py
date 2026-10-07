"""Coverage for judging decoded audio against the source clip in the encoded scenarios.

Matching the bytes the server sent against the bytes the client received only
proves the transport, so a server that sends part of the clip satisfies it. The
encoded scenarios also compare what the client decoded with the source, wherever
the codec is lossless and the client reports the evidence. Reproducing a short
send needs a server built to drop audio, so the rule is exercised here against
synthetic summaries shaped like the ones the adapters really write.
"""

from __future__ import annotations

import struct
import unittest
from typing import Any

from conformance.pcm import FloatPcmHasher
from conformance.runner import _compare_summaries
from conformance.scenarios import SCENARIOS, require_scenario

SOURCE_HASH = "a" * 64
OTHER_HASH = "b" * 64
ENCODED_HASH = "c" * 64
HEADER_HASH = "d" * 64
# The digest a client reports when it hashed no decoded samples at all.
EMPTY_HASH = FloatPcmHasher().hexdigest()
FRAME_COUNT = 39_744
CHUNK_COUNT = 69
# What the head of the stream costs when a client joins it late.
DROPPED_SAMPLES = 1_152


# Every server summary carries one, and a case whose summary lacks it never passes.
GROUP_UPDATE = {
    "type": "group/update",
    "payload": {"playback_state": "stopped", "group_id": "group-1", "group_name": "Kitchen"},
}


def _stream(codec: str, **overrides: Any) -> dict[str, Any]:
    return {"codec": codec, "sample_rate": 8_000, "channels": 1, "bit_depth": 16, **overrides}


def _server_summary(codec: str = "flac", **stream_overrides: Any) -> dict[str, Any]:
    return {
        "status": "ok",
        "implementation": "synthetic-server",
        "role": "server",
        "group_update": GROUP_UPDATE,
        "stream": {
            **_stream(codec, **stream_overrides),
            "codec_header_sha256": HEADER_HASH,
        },
        "audio": {
            "source_pcm_sha256": SOURCE_HASH,
            "sent_encoded_sha256": ENCODED_HASH,
            "sent_audio_chunk_count": CHUNK_COUNT,
            "sample_rate": 8_000,
            "channels": 1,
            "bit_depth": 16,
            "frame_count": FRAME_COUNT,
        },
    }


def _client_summary(codec: str = "flac", **audio_overrides: Any) -> dict[str, Any]:
    audio: dict[str, Any] = {
        "audio_chunk_count": CHUNK_COUNT,
        "received_encoded_sha256": ENCODED_HASH,
        "received_pcm_sha256": SOURCE_HASH,
        "received_sample_count": FRAME_COUNT,
        **audio_overrides,
    }
    return {
        "status": "ok",
        "implementation": "synthetic-client",
        "role": "client",
        "stream": {**_stream(codec), "codec_header_sha256": HEADER_HASH},
        "audio": audio,
    }


def _opus_server_summary() -> dict[str, Any]:
    """Return a server summary carrying the declared list the opus scenario also reads."""
    return {
        **_server_summary("opus"),
        "peer_hello": {
            "type": "client/hello",
            "payload": {
                "player@v1_support": {"supported_formats": [_stream("opus"), _stream("pcm")]}
            },
        },
    }


def _compare_flac(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
) -> tuple[bool, str]:
    return _compare_summaries(
        require_scenario("server-initiated-flac"),
        server_summary,
        client_summary,
    )


class DecodedFlacAgainstSourceTests(unittest.TestCase):
    def test_decoded_audio_matching_the_source_passes_and_says_so(self) -> None:
        matches, reason = _compare_flac(_server_summary(), _client_summary())
        self.assertTrue(matches)
        self.assertEqual(
            reason,
            "FLAC header and chunk bytes match exactly "
            "and the decoded audio matches the source clip",
        )

    def test_short_send_fails_although_sent_and_received_bytes_agree(self) -> None:
        matches, reason = _compare_flac(
            _server_summary(),
            _client_summary(
                received_pcm_sha256=OTHER_HASH,
                received_sample_count=FRAME_COUNT - DROPPED_SAMPLES,
            ),
        )
        self.assertFalse(matches)
        self.assertEqual(
            reason,
            "FLAC decoded audio does not match the source clip: "
            f"client received {FRAME_COUNT - DROPPED_SAMPLES} of {FRAME_COUNT} samples "
            f"({DROPPED_SAMPLES} fewer); source={SOURCE_HASH} client={OTHER_HASH}",
        )

    def test_equal_length_mismatch_reads_as_a_content_difference(self) -> None:
        matches, reason = _compare_flac(
            _server_summary(),
            _client_summary(received_pcm_sha256=OTHER_HASH),
        )
        self.assertFalse(matches)
        self.assertIn(f"sample counts equal ({FRAME_COUNT}), content differs", reason)

    def test_transport_mismatch_is_still_reported_as_one(self) -> None:
        """A corrupted stream also decodes wrongly; the cause is what gets named."""
        matches, reason = _compare_flac(
            _server_summary(),
            _client_summary(
                received_encoded_sha256=OTHER_HASH,
                received_pcm_sha256=OTHER_HASH,
            ),
        )
        self.assertFalse(matches)
        self.assertIn("FLAC chunk hash mismatch", reason)

    def test_missing_source_hash_fails_rather_than_going_unjudged(self) -> None:
        server = _server_summary()
        del server["audio"]["source_pcm_sha256"]
        matches, reason = _compare_flac(server, _client_summary())
        self.assertFalse(matches)
        self.assertEqual(reason, "Server summary is missing the source PCM hash")


class UnjudgedDecodedAudioTests(unittest.TestCase):
    """A rule that could not run still passes, and the reason admits it did not run."""

    def test_client_reporting_no_pcm_hash_is_not_judged(self) -> None:
        for label, overrides in (
            ("null hash", {"received_pcm_sha256": None, "received_sample_count": 0}),
            ("empty-input hash", {"received_pcm_sha256": EMPTY_HASH, "received_sample_count": 0}),
        ):
            with self.subTest(label):
                self._assert_unjudged(
                    _server_summary(),
                    _client_summary(**overrides),
                    "the client reported no decoded audio",
                )

    def test_client_omitting_the_pcm_hash_is_not_judged(self) -> None:
        client = _client_summary(received_sample_count=0)
        del client["audio"]["received_pcm_sha256"]
        self._assert_unjudged(_server_summary(), client, "the client reported no decoded audio")

    def test_non_decoding_client_is_not_judged_even_without_a_source_hash(self) -> None:
        """With nothing decoded there is nothing to compare, so the server is not blamed."""
        server = _server_summary()
        del server["audio"]["source_pcm_sha256"]
        self._assert_unjudged(
            server,
            _client_summary(received_pcm_sha256=None, received_sample_count=0),
            "the client reported no decoded audio",
        )

    def test_converted_stream_is_not_judged(self) -> None:
        self._assert_unjudged(
            _server_summary(sample_rate=48_000, channels=2),
            _client_summary(received_pcm_sha256=OTHER_HASH),
            "the stream was not negotiated at the source clip's format "
            "(sample_rate 8000 -> 48000, channels 1 -> 2)",
        )

    def test_wider_bit_depth_is_still_judged(self) -> None:
        matches, reason = _compare_flac(
            _server_summary(bit_depth=24),
            _client_summary(received_pcm_sha256=OTHER_HASH),
        )
        self.assertFalse(matches)
        self.assertIn("FLAC decoded audio does not match the source clip", reason)

    def test_widening_the_bit_depth_preserves_the_canonical_hash(self) -> None:
        """The premise that lets a 16-bit source be judged over a 24-bit stream."""
        samples = (-32_768, -1, 0, 1, 12_345, 32_767)
        narrow = FloatPcmHasher()
        narrow.update_from_pcm_bytes(struct.pack(f"<{len(samples)}h", *samples), bit_depth=16)
        wide = FloatPcmHasher()
        wide.update_from_pcm_bytes(
            b"".join((sample << 8).to_bytes(3, "little", signed=True) for sample in samples),
            bit_depth=24,
        )
        self.assertEqual(narrow.hexdigest(), wide.hexdigest())

    def test_opus_is_never_judged_by_hash(self) -> None:
        """Opus is lossy, so even a client that decodes it cannot match the source."""
        matches, reason = _compare_summaries(
            require_scenario("server-initiated-opus"),
            _opus_server_summary(),
            _client_summary("opus", received_pcm_sha256=OTHER_HASH),
        )
        self.assertTrue(matches)
        self.assertEqual(
            reason,
            "OPUS header and chunk bytes match exactly; decoded audio not judged against "
            "the source clip: OPUS is lossy, so its decoded audio cannot equal the source",
        )

    def test_every_encoded_scenario_states_whether_decoded_audio_was_judged(self) -> None:
        for scenario in SCENARIOS.values():
            if scenario.verification_mode != "audio-encoded-bytes":
                continue
            with self.subTest(scenario=scenario.id):
                codec = scenario.preferred_codec
                server_summary = (
                    _opus_server_summary() if codec == "opus" else _server_summary(codec)
                )
                matches, reason = _compare_summaries(
                    scenario, server_summary, _client_summary(codec)
                )
                self.assertTrue(matches)
                self.assertIn("decoded audio", reason)

    def _assert_unjudged(
        self,
        server_summary: dict[str, Any],
        client_summary: dict[str, Any],
        why: str,
    ) -> None:
        matches, reason = _compare_flac(server_summary, client_summary)
        self.assertTrue(matches)
        self.assertEqual(
            reason,
            "FLAC header and chunk bytes match exactly; "
            f"decoded audio not judged against the source clip: {why}",
        )


if __name__ == "__main__":
    unittest.main()
