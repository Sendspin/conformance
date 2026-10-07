"""The player audio chunk header and chunk duration, judged from the frames a case transported.

`roles/player/v1.md` fixes the binary audio chunk at a 13-byte header: message
type `4`, a big-endian int64 `timestamp`, then a big-endian uint32 `send_ahead`.
It also bounds what a server may put behind it: a chunk MUST NOT be longer than
150 ms and SHOULD NOT be shorter than 15 ms, the final chunk of a stream
excepted.

Adapters do not report a header. They report each audio frame as it crossed the
transport, as its size and its leading bytes, beside the size of the audio that
same frame carried, counted where the adapter held the audio on its own. The
header of a frame is whatever it carries beyond that audio, so every frame is
judged by itself and none by an average. An adapter that cannot see a raw
frame, or cannot tell which audio a frame carried, reports that it cannot, and
the case fails naming a harness gap rather than a fault in either
implementation.

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

from dataclasses import dataclass
from typing import Any

from .declared_formats import normalized_format

HEADER_BYTES = 13
AUDIO_CHUNK_TYPE = 4
MAX_CHUNK_US = 150_000
MIN_CHUNK_US = 15_000

HARNESS_GAP = "Harness gap, not a protocol result: "


@dataclass(frozen=True)
class Frame:
    """One binary frame as an adapter saw it cross the transport."""

    byte_count: int
    leading: bytes
    # The audio this frame carried, or None where the adapter could not tell.
    payload_byte_count: int | None

    @property
    def header_bytes(self) -> int | None:
        """Return the bytes this frame carries beyond its audio, or None if that is unknown."""
        if self.payload_byte_count is None:
            return None
        return self.byte_count - self.payload_byte_count


def transported_frames(value: Any) -> list[Frame] | None:
    """Return the frames a summary field records, or None.

    None means the field does not hold usable frame records, which covers an
    adapter that reported `null` because it cannot observe raw frames.
    """
    if not isinstance(value, list):
        return None
    frames: list[Frame] = []
    for record in value:
        if not isinstance(record, dict):
            return None
        byte_count = _count(record.get("byte_count"))
        leading_hex = record.get("leading_hex")
        if byte_count is None or not isinstance(leading_hex, str):
            return None
        try:
            leading = bytes.fromhex(leading_hex)
        except ValueError:
            return None
        if not leading or len(leading) != min(byte_count, HEADER_BYTES):
            return None
        payload_byte_count = _count(record.get("payload_byte_count"))
        if payload_byte_count is not None and payload_byte_count > byte_count:
            return None
        frames.append(Frame(byte_count, leading, payload_byte_count))
    return frames


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

    for index, frame in enumerate(sent, start=1):
        if frame.leading[0] != AUDIO_CHUNK_TYPE:
            return False, (
                f"Server sent binary message type {frame.leading[0]} as frame {index} of "
                f"{len(sent)} in a player-only stream; roles/player/v1.md fixes an audio "
                f"chunk at type {AUDIO_CHUNK_TYPE}"
            )

    violation = _header_violation("server", sent)
    if violation is not None:
        # A server that framed the stream this way on purpose says why, and the
        # reason can put the cause on the client it was accommodating.
        stated = server_audio.get("legacy_header_reason")
        if isinstance(stated, str) and stated and not violation.startswith(HARNESS_GAP):
            violation += f"; server adapter reported: {stated}"
        return False, violation

    passed, durations = _chunk_durations_verdict(server_summary, server_audio, sent)
    if not passed:
        return False, durations

    client_audio = client_summary.get("audio")
    if not isinstance(client_audio, dict):
        return False, "Client summary is missing an 'audio' section"
    received = transported_frames(client_audio.get("received_chunk_frames"))
    if received is None:
        return False, _unobserved_client_verdict(client_audio, sent)
    if len(received) != len(sent):
        return False, (
            f"Client received {len(received)} of the {len(sent)} audio chunk frames the "
            "server sent"
        )
    for index, (ours, theirs) in enumerate(zip(sent, received), start=1):
        # Both sides record the same plaintext, so only a recorder can differ.
        if (ours.byte_count, ours.leading) != (theirs.byte_count, theirs.leading):
            return False, (
                f"{HARNESS_GAP}the adapters disagree about audio chunk frame {index} of "
                f"{len(sent)}, which the transport delivers unchanged"
            )
    violation = _header_violation("client", received)
    if violation is not None:
        return False, violation

    send_ahead = [int.from_bytes(frame.leading[9:13], "big") for frame in sent]
    return True, (
        f"All {len(sent)} audio chunks carried the {HEADER_BYTES}-byte header; {durations}; "
        f"send_ahead read {min(send_ahead)}-{max(send_ahead)} us, its saturation not judged"
    )


def _count(value: Any) -> int | None:
    """Return a byte or chunk count from a summary field, or None when it is not one."""
    # bool is an int subclass, and a summary field can hold any JSON value.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _header_violation(role: str, frames: list[Frame]) -> str | None:
    """Return why these frames do not each carry the RC1 header, or None when they do."""
    unpaired = sum(frame.header_bytes is None for frame in frames)
    if unpaired:
        return (
            f"{HARNESS_GAP}the {role} adapter could not tell which audio {unpaired} of its "
            f"{len(frames)} frames carried, so their headers cannot be measured"
        )
    wrong = [frame.header_bytes for frame in frames if frame.header_bytes != HEADER_BYTES]
    if not wrong:
        return None
    widths = ", ".join(str(width) for width in sorted(set(wrong)))
    verb = "sent" if role == "server" else "read"
    return (
        f"{role.capitalize()} {verb} a header of {widths} bytes on {len(wrong)} of "
        f"{len(frames)} audio chunks; roles/player/v1.md requires {HEADER_BYTES} bytes "
        "(type, int64 timestamp, uint32 send_ahead)"
    )


def _unobserved_client_verdict(client_audio: dict[str, Any], sent: list[Frame]) -> str:
    """Explain a case whose client adapter saw no raw frame.

    The total it delivered can still convict it: every frame the server sent
    held a known amount of audio, so a client that delivered any other amount
    took the wrong number of bytes off at least one of them. A matching total
    shows nothing about any one frame and passes nothing.
    """
    sent_audio = sum(frame.payload_byte_count or 0 for frame in sent)
    delivered = _count(client_audio.get("received_encoded_byte_count"))
    if delivered is not None and delivered != sent_audio:
        return (
            f"Client delivered {delivered} bytes of audio from {len(sent)} chunks that "
            f"carried {sent_audio} behind a {HEADER_BYTES}-byte header each, so it did not "
            f"read {HEADER_BYTES} bytes off every one"
        )
    return (
        f"{HARNESS_GAP}the client adapter does not observe raw audio chunk frames, so "
        f"the header the client read off each one cannot be judged; the server sent "
        f"{HEADER_BYTES} bytes on every chunk"
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
    bytes_per_second = 0
    if stream is not None and stream["bit_depth"] > 0 and stream["bit_depth"] % 8 == 0:
        bytes_per_second = stream["sample_rate"] * stream["channels"] * (stream["bit_depth"] // 8)
    if stream is None or bytes_per_second <= 0:
        return False, (
            f"{HARNESS_GAP}the server adapter reported no usable stream format, so chunk "
            f"durations cannot be read from the audio each frame carried "
            f"(stream={server_summary.get('stream')!r})"
        )
    if stream["codec"] != "pcm":
        return False, (
            f"Server negotiated {stream['codec']} where the client listed only PCM, so "
            "chunk durations cannot be read from the audio each frame carried"
        )
    durations_us = [
        (frame.payload_byte_count or 0) * 1_000_000 / bytes_per_second for frame in sent
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
