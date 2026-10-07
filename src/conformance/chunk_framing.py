"""The player audio chunk header and chunk duration, judged from the frames a case transported.

`roles/player/v1.md` fixes the binary audio chunk at a 13-byte header: message
type `4`, a big-endian int64 `timestamp`, then a big-endian uint32 `send_ahead`.
It also bounds what a server may put behind it: a chunk MUST NOT be longer than
150 ms and SHOULD NOT be shorter than 15 ms, the final chunk of a stream
excepted.

Adapters do not report a header. They report each audio frame as it crossed the
transport, as its size and its leading bytes, and this module derives the rest:
the header width is whatever a frame carries beyond its audio, taken from the
frame sizes and the payload bytes the same adapter counted separately. An
adapter that cannot see a raw frame reports none, and the case fails naming that
as a harness gap rather than as a fault in either implementation.

Three things the frames show are deliberately left unjudged:

- `send_ahead` saturation. A server MUST send `0` when it transmits at or after
  `timestamp` and `4294967295` when the lead is too long to represent. Telling
  either from an honest value needs the instant the frame was transmitted,
  which no summary carries, and the stream this scenario drives is sent a
  fraction of a second early, so neither branch is exercised.
- The byte order of `timestamp` and `send_ahead`. Nothing a case transports
  fixes the value either field should hold.
- Chunk duration where the server adapter cut the chunks itself. That says how
  the adapter was written, not how the implementation frames a stream.
"""

from __future__ import annotations

from typing import Any

from .declared_formats import normalized_format

HEADER_BYTES = 13
AUDIO_CHUNK_TYPE = 4
MAX_CHUNK_US = 150_000
MIN_CHUNK_US = 15_000

HARNESS_GAP = "Harness gap, not a protocol result: "

Frame = tuple[int, bytes]


def transported_frames(value: Any) -> list[Frame] | None:
    """Return the `(byte_count, leading_bytes)` frames a summary field records, or None.

    None means the field does not hold usable frame records, which covers an
    adapter that reported `null` because it cannot observe raw frames.
    """
    if not isinstance(value, list):
        return None
    frames: list[Frame] = []
    for record in value:
        if not isinstance(record, dict):
            return None
        byte_count = record.get("byte_count")
        leading_hex = record.get("leading_hex")
        # bool is an int subclass, and a summary field can hold any JSON value.
        if isinstance(byte_count, bool) or not isinstance(byte_count, int):
            return None
        if not isinstance(leading_hex, str):
            return None
        try:
            leading = bytes.fromhex(leading_hex)
        except ValueError:
            return None
        if not leading or len(leading) != min(byte_count, HEADER_BYTES):
            return None
        frames.append((byte_count, leading))
    return frames


def header_width(frames: list[Frame], *, payload_byte_count: Any, chunk_count: Any) -> int | None:
    """Return the bytes each frame carries beyond its audio payload, or None.

    None means the counts do not describe the same chunks as `frames`, or do not
    divide into a whole header per chunk, so no width can be read from them.
    """
    for count in (payload_byte_count, chunk_count):
        if isinstance(count, bool) or not isinstance(count, int):
            return None
    if not frames or chunk_count != len(frames):
        return None
    overhead = sum(byte_count for byte_count, _ in frames) - payload_byte_count
    if overhead < 0 or overhead % len(frames):
        return None
    return overhead // len(frames)


def chunk_framing_verdict(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
) -> tuple[bool, str]:
    """Judge a case's audio chunk header and chunk durations, as (passed, reason).

    What the server sent is judged first, so a server fault is reported even
    when the client adapter has no raw frames to offer.
    """
    if server_summary.get("status") != "ok":
        return False, f"Server summary status is {server_summary.get('status')!r}"
    if client_summary.get("status") != "ok":
        return False, f"Client summary status is {client_summary.get('status')!r}"

    server_audio = server_summary.get("audio")
    if not isinstance(server_audio, dict):
        return False, "Server summary is missing an 'audio' section"
    sent = transported_frames(server_audio.get("sent_chunk_frames"))
    if sent is None:
        return False, (
            f"{HARNESS_GAP}the server adapter did not observe the audio chunk frames it "
            "sent, so the header on the wire cannot be judged"
        )
    if not sent:
        return False, "Server sent no audio chunks"

    for index, (_, leading) in enumerate(sent, start=1):
        if leading[0] != AUDIO_CHUNK_TYPE:
            return False, (
                f"Server sent binary message type {leading[0]} as frame {index} of "
                f"{len(sent)} in a player-only stream; roles/player/v1.md fixes an audio "
                f"chunk at type {AUDIO_CHUNK_TYPE}"
            )

    sent_width = header_width(
        sent,
        payload_byte_count=server_audio.get("sent_encoded_byte_count"),
        chunk_count=server_audio.get("sent_audio_chunk_count"),
    )
    if sent_width is None:
        return False, _unreadable_width(
            "server",
            sent,
            payload_byte_count=server_audio.get("sent_encoded_byte_count"),
            chunk_count=server_audio.get("sent_audio_chunk_count"),
        )
    if sent_width != HEADER_BYTES:
        # A server that framed the stream this way on purpose says why, and the
        # reason can put the cause on the client it was accommodating.
        stated = server_audio.get("legacy_header_reason")
        return False, (
            f"Server sent a {sent_width}-byte audio chunk header; roles/player/v1.md "
            f"requires {HEADER_BYTES} bytes (type, int64 timestamp, uint32 send_ahead)"
            + (f"; server adapter reported: {stated}" if isinstance(stated, str) and stated else "")
        )

    passed, durations = _chunk_durations_verdict(server_summary, server_audio, sent)
    if not passed:
        return False, durations

    client_audio = client_summary.get("audio")
    if not isinstance(client_audio, dict):
        return False, "Client summary is missing an 'audio' section"
    delivered = {
        "payload_byte_count": client_audio.get("received_encoded_byte_count"),
        "chunk_count": client_audio.get("audio_chunk_count"),
    }
    received = transported_frames(client_audio.get("received_chunk_frames"))
    if received is None:
        # Without its own frames the client is still held to the ones the server
        # sent: delivering any other byte count out of them is a header misread.
        stripped = header_width(sent, **delivered)
        if stripped is not None and stripped != HEADER_BYTES:
            return False, _client_width_violation(stripped)
        return False, (
            f"{HARNESS_GAP}the client adapter does not observe raw audio chunk frames, "
            f"so the header the client received cannot be judged; the server sent "
            f"{HEADER_BYTES} bytes"
            + (
                " and the client delivered exactly the audio behind them"
                if stripped == HEADER_BYTES
                else ""
            )
        )
    if len(received) != len(sent):
        return False, (
            f"Client received {len(received)} of the {len(sent)} audio chunk frames the "
            "server sent"
        )
    if received != sent:
        # Both sides record the same plaintext, so only a recorder can differ.
        differing = next(
            index for index, pair in enumerate(zip(sent, received), start=1) if pair[0] != pair[1]
        )
        return False, (
            f"{HARNESS_GAP}the adapters disagree about audio chunk frame {differing} of "
            f"{len(sent)}, which the transport delivers unchanged"
        )
    received_width = header_width(received, **delivered)
    if received_width is None:
        return False, _unreadable_width("client", received, **delivered)
    if received_width != HEADER_BYTES:
        return False, _client_width_violation(received_width)

    send_ahead = [int.from_bytes(leading[9:13], "big") for _, leading in sent]
    return True, (
        f"{len(sent)} audio chunks carried the {HEADER_BYTES}-byte header; {durations}; "
        f"send_ahead read {min(send_ahead)}-{max(send_ahead)} us, its saturation not judged"
    )


def _unreadable_width(
    role: str, frames: list[Frame], *, payload_byte_count: Any, chunk_count: Any
) -> str:
    if payload_byte_count is None:
        return (
            f"{HARNESS_GAP}the {role} adapter did not count the audio payload apart from "
            "its header, so the header width cannot be read from the transported frames"
        )
    return (
        f"{HARNESS_GAP}the {role} adapter counted {chunk_count!r} chunks and "
        f"{payload_byte_count!r} payload bytes against {len(frames)} transported frames, "
        "which gives no whole header per chunk"
    )


def _client_width_violation(width: int) -> str:
    return (
        f"Client read a {width}-byte header off each audio chunk; roles/player/v1.md "
        f"requires {HEADER_BYTES} bytes (type, int64 timestamp, uint32 send_ahead)"
    )


def _chunk_durations_verdict(
    server_summary: dict[str, Any],
    server_audio: dict[str, Any],
    sent: list[Frame],
) -> tuple[bool, str]:
    """Judge the duration of each sent chunk, as (passed, violation or finding)."""
    chunked_by = server_audio.get("chunked_by")
    if chunked_by == "adapter":
        return True, (
            "chunk durations not judged, the server adapter having cut the chunks itself"
        )
    if chunked_by != "implementation":
        return False, (
            f"{HARNESS_GAP}the server adapter does not say whether it or the "
            "implementation cut the audio into chunks, so their durations cannot be "
            "attributed"
        )

    stream = normalized_format(server_summary.get("stream"))
    if stream is None:
        return False, (
            f"{HARNESS_GAP}the server adapter reported no usable stream format, so chunk "
            f"durations cannot be read from the frame sizes "
            f"(stream={server_summary.get('stream')!r})"
        )
    if stream["codec"] != "pcm":
        return False, (
            f"Server negotiated {stream['codec']} where the client listed only PCM, so "
            "chunk durations cannot be read from the frame sizes"
        )
    bytes_per_second = stream["sample_rate"] * stream["channels"] * (stream["bit_depth"] // 8)
    durations_us = [
        (byte_count - HEADER_BYTES) * 1_000_000 / bytes_per_second for byte_count, _ in sent
    ]
    for index, duration_us in enumerate(durations_us, start=1):
        if duration_us > MAX_CHUNK_US:
            return False, (
                f"Server sent audio chunk {index} of {len(sent)} lasting "
                f"{duration_us / 1_000:.1f} ms; roles/player/v1.md says a server MUST NOT "
                "send a chunk longer than 150 ms"
            )
        # The chunk before a format change may also run short, and this scenario
        # drives a single format, so only the last chunk is excused.
        if duration_us < MIN_CHUNK_US and index != len(sent):
            return False, (
                f"Server sent audio chunk {index} of {len(sent)} lasting "
                f"{duration_us / 1_000:.1f} ms; roles/player/v1.md says a server SHOULD NOT "
                "send a chunk shorter than 15 ms unless it ends the stream"
            )
    return True, (
        f"each lasted {min(durations_us) / 1_000:.1f}-{max(durations_us) / 1_000:.1f} ms"
    )
