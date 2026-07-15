"""aiosendspin server adapter for conformance scenarios."""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from conformance.flac import (
    decode_fixture,
    flac_encoder_frame_samples,
    trim_fixture_to_frame_multiple,
)
from conformance.io import write_json
from conformance.registry import lookup_endpoint, register_endpoint


def _add_repo_to_syspath(dirname: str) -> None:
    from conformance.implementations import resolve_required_repo_path

    repo = resolve_required_repo_path(dirname)
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-name", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--scenario-id", default="server-initiated-flac")
    parser.add_argument("--initiator-role", choices=("server", "client"), default="server")
    parser.add_argument("--preferred-codec", default="flac")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--port", type=int, default=8927)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--server-id", default="conformance-server")
    parser.add_argument("--server-name", default="Sendspin Conformance Server")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--enable-mdns", action="store_true")
    parser.add_argument("--clip-seconds", type=float, default=5.0)
    parser.add_argument("--metadata-title", default="Almost Silent")
    parser.add_argument("--metadata-artist", default="Sendspin Conformance")
    parser.add_argument("--metadata-album-artist", default="Sendspin")
    parser.add_argument("--metadata-album", default="Protocol Fixtures")
    parser.add_argument("--metadata-artwork-url", default="https://example.invalid/almost-silent.jpg")
    parser.add_argument("--metadata-year", type=int, default=2026)
    parser.add_argument("--metadata-track", type=int, default=1)
    parser.add_argument("--metadata-track-progress", type=int, default=12_000)
    parser.add_argument("--metadata-track-duration", type=int, default=180_000)
    parser.add_argument("--metadata-playback-speed", type=int, default=1_000)
    parser.add_argument("--controller-command", default="next")
    parser.add_argument("--controller-repeat", default="all")
    parser.add_argument("--controller-shuffle", default="false")
    parser.add_argument("--artwork-format", default="jpeg")
    parser.add_argument("--artwork-width", type=int, default=256)
    parser.add_argument("--artwork-height", type=int, default=256)
    return parser


def _find_connected_client(server: Any, client_name: str) -> Any | None:
    for client in server.connected_clients:
        if client.name == client_name:
            return client
    return None


async def _wait_for_target_client(
    server: Any,
    *,
    client_name: str,
    registry_path: Path,
    timeout_s: float,
) -> tuple[Any, str]:
    from aiosendspin.models.types import ConnectionReason

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    fallback_attempted = False
    while loop.time() < deadline:
        target = _find_connected_client(server, client_name)
        if target is not None:
            method = "registry_fallback" if fallback_attempted else "mdns"
            return target, method

        if not fallback_attempted:
            registry_url = lookup_endpoint(registry_path, client_name)
            if registry_url is not None:
                fallback_attempted = True
                await server.connect_to_client_and_wait(
                    registry_url,
                    connection_reason=ConnectionReason.PLAYBACK,
                )
                continue

        await asyncio.sleep(0.1)

    raise TimeoutError(f"Timed out waiting for client {client_name!r}")


async def _wait_for_incoming_client(
    server: Any,
    *,
    client_name: str,
    timeout_s: float,
) -> tuple[Any, str]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        target = _find_connected_client(server, client_name)
        if target is not None:
            return target, "registry_advertised"
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for client {client_name!r}")


def _expand_pcm_16_to_32(pcm_16_bytes: bytes) -> bytes:
    """Re-pack signed 16-bit little-endian PCM as signed 32-bit little-endian.

    Each 16-bit sample becomes a 32-bit sample of equal float value by
    left-shifting 16 bits — the original two bytes become the high half of the
    32-bit sample and two zero bytes fill the low half. ``FloatPcmHasher``
    produces identical float-domain hashes for the two representations, so
    existing PCM verification still works. The aiosendspin SDK ingests 32-bit
    PCM as the PyAV-compatible source for any non-16-bit wire format,
    including the 24-bit packed wire format the SDK then writes on the wire.
    """
    sample_count = len(pcm_16_bytes) // 2
    out = bytearray(sample_count * 4)
    out[2::4] = pcm_16_bytes[0::2]
    out[3::4] = pcm_16_bytes[1::2]
    return bytes(out)


def _iter_pcm_blocks(
    pcm_bytes: bytes,
    *,
    sample_rate: int,
    channels: int,
    bit_depth: int,
    block_ms: int = 100,
) -> list[tuple[bytes, int]]:
    bytes_per_frame = channels * (bit_depth // 8)
    frames_per_block = max(1, round(sample_rate * (block_ms / 1000.0)))
    bytes_per_block = frames_per_block * bytes_per_frame
    chunks: list[tuple[bytes, int]] = []
    for offset in range(0, len(pcm_bytes), bytes_per_block):
        chunk = pcm_bytes[offset : offset + bytes_per_block]
        frame_count = len(chunk) // bytes_per_frame
        duration_us = int(frame_count * 1_000_000 / sample_rate)
        chunks.append((chunk, duration_us))
    return chunks


def _bool_from_cli(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _metadata_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "title": args.metadata_title,
        "artist": args.metadata_artist,
        "album_artist": args.metadata_album_artist,
        "album": args.metadata_album,
        "artwork_url": args.metadata_artwork_url,
        "year": args.metadata_year,
        "track": args.metadata_track,
        "progress": {
            "track_progress": args.metadata_track_progress,
            "track_duration": args.metadata_track_duration,
            "playback_speed": args.metadata_playback_speed,
        },
    }


def _reference_artwork_image() -> Image.Image:
    image = Image.new("RGB", (320, 200), "#e8d4b8")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 320, 60), fill="#c9783b")
    draw.rectangle((0, 60, 320, 130), fill="#935228")
    draw.rectangle((0, 130, 320, 200), fill="#4b2f1b")
    draw.ellipse((24, 22, 114, 112), fill="#f3e5cf", outline="#4b2f1b", width=4)
    draw.rectangle((150, 36, 280, 54), fill="#f3e5cf")
    draw.rectangle((150, 74, 252, 88), fill="#e7c49f")
    draw.rectangle((150, 102, 264, 116), fill="#e7c49f")
    draw.rectangle((150, 140, 296, 166), fill="#d7a16d")
    draw.line((152, 153, 292, 153), fill="#4b2f1b", width=3)
    return image


def _picture_format(raw: str) -> Any:
    from aiosendspin.models.types import PictureFormat

    normalized = raw.strip().lower()
    if normalized == "jpeg":
        return PictureFormat.JPEG
    if normalized == "png":
        return PictureFormat.PNG
    if normalized == "bmp":
        return PictureFormat.BMP
    raise ValueError(f"Unsupported artwork format: {raw}")


def _encode_artwork(image: Image.Image, width: int, height: int, art_format: Any) -> bytes:
    from aiosendspin.models.types import PictureFormat

    image_aspect = image.width / image.height
    target_aspect = width / height
    if image_aspect > target_aspect:
        new_width = width
        new_height = int(width / image_aspect)
    else:
        new_height = height
        new_width = int(height * image_aspect)
    resized = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
    letterboxed = Image.new("RGB", (width, height), (0, 0, 0))
    x_offset = (width - new_width) // 2
    y_offset = (height - new_height) // 2
    letterboxed.paste(resized, (x_offset, y_offset))

    from io import BytesIO

    with BytesIO() as buf:
        if art_format == PictureFormat.JPEG:
            letterboxed.save(buf, format="JPEG", quality=85)
        elif art_format == PictureFormat.PNG:
            letterboxed.save(buf, format="PNG", compress_level=6)
        elif art_format == PictureFormat.BMP:
            letterboxed.save(buf, format="BMP")
        else:
            raise NotImplementedError(f"Unsupported artwork format: {art_format}")
        buf.seek(0)
        return buf.read()


async def _disconnect_client(client: Any) -> None:
    connection = client.connection
    if connection is None:
        return
    await connection.disconnect(retry_connection=False)


def _controller_command_payload(command: str) -> dict[str, Any]:
    return {"command": command}


def _controller_event_to_command(event: Any) -> dict[str, Any] | None:
    name = type(event).__name__
    if name == "ControllerPlayEvent":
        return {"command": "play"}
    if name == "ControllerPauseEvent":
        return {"command": "pause"}
    if name == "ControllerStopEvent":
        return {"command": "stop"}
    if name == "ControllerNextEvent":
        return {"command": "next"}
    if name == "ControllerPreviousEvent":
        return {"command": "previous"}
    if name == "ControllerSwitchEvent":
        return {"command": "switch"}
    if name == "ControllerRepeatEvent":
        mode = getattr(event, "mode", None)
        return {"command": f"repeat_{str(mode.value if mode is not None else mode)}"}
    if name == "ControllerShuffleEvent":
        shuffle = bool(getattr(event, "shuffle", False))
        return {"command": "shuffle" if shuffle else "unshuffle"}
    if name == "ControllerVolumeEvent":
        return {"command": "volume", "volume": int(getattr(event, "volume", 0))}
    if name == "ControllerMuteEvent":
        return {"command": "mute", "mute": bool(getattr(event, "muted", False))}
    return None


def _client_snapshot(client: Any) -> dict[str, Any]:
    return {
        "client_id": client.client_id,
        "name": client.name,
        "supported_roles": list(client.info.supported_roles),
        "active_roles": list(client.negotiated_roles),
    }


def _base_summary(
    args: argparse.Namespace,
    *,
    discovery_method: str,
    client: Any,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "implementation": "aiosendspin",
        "role": "server",
        "server_id": args.server_id,
        "server_name": args.server_name,
        "scenario_id": args.scenario_id,
        "initiator_role": args.initiator_role,
        "preferred_codec": args.preferred_codec,
        "discovery_method": discovery_method,
        "peer_hello": {
            "type": "client/hello",
            "payload": client.info.to_dict(),
        },
        "client": _client_snapshot(client),
    }


async def _run_audio_scenario(args: argparse.Namespace, *, server: Any, client: Any) -> dict[str, Any]:
    from aiosendspin.models import BINARY_HEADER_SIZE
    from aiosendspin.models.core import StreamStartMessage
    from aiosendspin.models.types import BinaryMessageType
    from aiosendspin.server.audio import AudioFormat

    fixture = decode_fixture(Path(args.fixture), max_duration_seconds=args.clip_seconds)
    frame_alignment_samples: int | None = None
    trimmed_source_frames = 0
    if args.preferred_codec == "flac":
        frame_alignment_samples = flac_encoder_frame_samples(
            sample_rate=fixture.sample_rate,
            bit_depth=fixture.bit_depth,
            channels=fixture.channels,
        )
        fixture, trimmed_source_frames = trim_fixture_to_frame_multiple(
            fixture,
            frame_samples=frame_alignment_samples,
        )
    wire_bit_depth = fixture.bit_depth
    source_bit_depth = fixture.bit_depth
    source_pcm_bytes = fixture.pcm_bytes
    if args.scenario_id == "server-initiated-pcm-24bit":
        # The SDK ingests source PCM as s32 (PyAV-compatible) and emits the
        # negotiated 24-bit packed format on the wire. Feed 32-bit source
        # bytes here and declare bit_depth=32 as the source format; the
        # negotiated wire bit depth is reported via stream/start.
        source_pcm_bytes = _expand_pcm_16_to_32(fixture.pcm_bytes)
        source_bit_depth = 32
        wire_bit_depth = 24
    stream_state: dict[str, Any] | None = None
    sent_codec_header_sha256: str | None = None
    sent_audio_hasher = sha256()
    sent_audio_chunk_count = 0
    sent_audio_byte_count = 0

    def capture_stream_start(message: Any) -> None:
        nonlocal stream_state, sent_codec_header_sha256
        if not isinstance(message, StreamStartMessage) or message.payload.player is None:
            return
        player = message.payload.player
        stream_state = {
            "codec": player.codec.value,
            "sample_rate": player.sample_rate,
            "channels": player.channels,
            "bit_depth": player.bit_depth,
            "codec_header": player.codec_header,
        }
        if player.codec_header:
            try:
                sent_codec_header_sha256 = sha256(base64.b64decode(player.codec_header)).hexdigest()
                stream_state["codec_header_sha256"] = sent_codec_header_sha256
            except Exception:
                sent_codec_header_sha256 = None

    original_send_message = client.send_message
    original_send_role_message = client.send_role_message
    original_send_binary = client.send_binary

    def send_message_wrapper(message: Any) -> None:
        capture_stream_start(message)
        original_send_message(message)

    def send_role_message_wrapper(role: str, message: Any) -> None:
        capture_stream_start(message)
        original_send_role_message(role, message)

    def send_binary_wrapper(
        data: bytes,
        *,
        role_family: str,
        timestamp_us: int,
        message_type: int,
        buffer_end_time_us: int | None = None,
        buffer_byte_count: int | None = None,
        duration_us: int | None = None,
    ) -> None:
        nonlocal sent_audio_chunk_count, sent_audio_byte_count
        if message_type == BinaryMessageType.AUDIO_CHUNK.value:
            payload = data[BINARY_HEADER_SIZE:]
            sent_audio_hasher.update(payload)
            sent_audio_chunk_count += 1
            sent_audio_byte_count += len(payload)
        original_send_binary(
            data,
            role_family=role_family,
            timestamp_us=timestamp_us,
            message_type=message_type,
            buffer_end_time_us=buffer_end_time_us,
            buffer_byte_count=buffer_byte_count,
            duration_us=duration_us,
        )

    client.send_message = send_message_wrapper  # type: ignore[method-assign]
    client.send_role_message = send_role_message_wrapper  # type: ignore[method-assign]
    client.send_binary = send_binary_wrapper  # type: ignore[method-assign]

    try:
        stream = client.group.start_stream()
        audio_format = AudioFormat(
            sample_rate=fixture.sample_rate,
            bit_depth=source_bit_depth,
            channels=fixture.channels,
        )
        next_play_start_us = server.clock.now_us() + 250_000
        total_duration_us = 0
        # Renegotiation scenarios need audio to keep flowing in real time so a
        # mid-stream stream/request-format has later chunks to flush the new
        # stream/start. Other scenarios commit upfront and settle once at the end.
        pace_realtime = args.scenario_id in {
            "client-initiated-request-format-pcm",
            "client-initiated-request-format-flac",
        }
        for chunk, duration_us in _iter_pcm_blocks(
            source_pcm_bytes,
            sample_rate=fixture.sample_rate,
            channels=fixture.channels,
            bit_depth=source_bit_depth,
        ):
            stream.prepare_audio(chunk, audio_format)
            play_start_us = await stream.commit_audio(play_start_us=next_play_start_us)
            next_play_start_us = play_start_us + duration_us
            total_duration_us += duration_us
            if pace_realtime:
                await asyncio.sleep(duration_us / 1_000_000.0)
        settle_seconds = 0.75 if pace_realtime else (total_duration_us / 1_000_000.0) + 0.75
        await asyncio.sleep(settle_seconds)
        await client.group.stop()
        await asyncio.sleep(0.5)
        await _disconnect_client(client)
    finally:
        client.send_message = original_send_message  # type: ignore[method-assign]
        client.send_role_message = original_send_role_message  # type: ignore[method-assign]
        client.send_binary = original_send_binary  # type: ignore[method-assign]

    return {
        "stream": stream_state,
        "audio": {
            "fixture": str(fixture.path),
            "source_flac_sha256": fixture.source_flac_sha256,
            "source_pcm_sha256": fixture.source_pcm_sha256,
            "sent_codec_header_sha256": sent_codec_header_sha256,
            "sent_encoded_sha256": sent_audio_hasher.hexdigest() if sent_audio_chunk_count else None,
            "sent_audio_chunk_count": sent_audio_chunk_count,
            "sent_encoded_byte_count": sent_audio_byte_count,
            "clip_seconds": args.clip_seconds,
            "sample_rate": fixture.sample_rate,
            "channels": fixture.channels,
            "bit_depth": wire_bit_depth,
            "frame_count": fixture.frame_count,
            "duration_seconds": fixture.duration_seconds,
            "frame_alignment_samples": frame_alignment_samples,
            "trimmed_source_frames": trimmed_source_frames,
        }
    }


async def _run_metadata_scenario(args: argparse.Namespace, *, client: Any) -> dict[str, Any]:
    from aiosendspin.server.roles.metadata import MetadataGroupRole

    metadata_group_role = client.group.group_role("metadata")
    if not isinstance(metadata_group_role, MetadataGroupRole):
        raise RuntimeError("Metadata group role is not active for this client")

    expected = _metadata_snapshot(args)
    metadata_group_role.update(
        title=args.metadata_title,
        artist=args.metadata_artist,
        album_artist=args.metadata_album_artist,
        album=args.metadata_album,
        artwork_url=args.metadata_artwork_url,
        year=args.metadata_year,
        track=args.metadata_track,
        track_progress=args.metadata_track_progress,
        track_duration=args.metadata_track_duration,
        playback_speed=args.metadata_playback_speed,
    )
    await asyncio.sleep(0.5)
    await _disconnect_client(client)
    return {"metadata": {"expected": expected}}


async def _run_controller_scenario(args: argparse.Namespace, *, client: Any) -> dict[str, Any]:
    from aiosendspin.models.types import MediaCommand, RepeatMode
    from aiosendspin.server.roles.controller import ControllerGroupRole

    controller_group_role = client.group.group_role("controller")
    if not isinstance(controller_group_role, ControllerGroupRole):
        raise RuntimeError("Controller group role is not active for this client")

    expected_command = _controller_command_payload(args.controller_command)
    event_future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

    def on_group_event(_group: Any, event: Any) -> None:
        command = _controller_event_to_command(event)
        if command is not None and not event_future.done():
            event_future.set_result(command)

    unsubscribe = client.group.add_event_listener(on_group_event)
    try:
        # repeat/shuffle are controller-role state; advertise them so the client
        # observes them in the controller state update.
        controller_group_role.set_repeat(RepeatMode(args.controller_repeat))
        controller_group_role.set_shuffle(_bool_from_cli(args.controller_shuffle))
        controller_group_role.set_supported_commands([MediaCommand(args.controller_command)])
        received_command = await asyncio.wait_for(event_future, timeout=args.timeout_seconds)
    finally:
        unsubscribe()

    await asyncio.sleep(0.2)
    await _disconnect_client(client)
    protocol_commands = {MediaCommand.VOLUME, MediaCommand.MUTE, MediaCommand.SWITCH}
    app_commands = {MediaCommand(args.controller_command)}
    all_commands = sorted((protocol_commands | app_commands), key=lambda c: c.value)
    return {
        "controller": {
            "expected_command": expected_command,
            "received_command": received_command,
            "supported_commands": [command.value for command in all_commands],
            "volume": controller_group_role.volume,
            "muted": controller_group_role.muted,
            "repeat": controller_group_role.repeat.value,
            "shuffle": controller_group_role.shuffle,
        }
    }


async def _run_artwork_scenario(args: argparse.Namespace, *, client: Any) -> dict[str, Any]:
    from aiosendspin.server.roles.artwork import ArtworkGroupRole

    artwork_group_role = client.group.group_role("artwork")
    if not isinstance(artwork_group_role, ArtworkGroupRole):
        raise RuntimeError("Artwork group role is not active for this client")

    image = _reference_artwork_image()
    art_format = _picture_format(args.artwork_format)
    encoded = _encode_artwork(
        image.copy(),
        args.artwork_width,
        args.artwork_height,
        art_format,
    )
    await artwork_group_role.set_album_artwork(image)
    await asyncio.sleep(0.5)
    await _disconnect_client(client)
    return {
        "artwork": {
            "channel": 0,
            "source": "album",
            "format": args.artwork_format.lower(),
            "width": args.artwork_width,
            "height": args.artwork_height,
            "encoded_sha256": sha256(encoded).hexdigest(),
            "byte_count": len(encoded),
        }
    }


async def _scenario_payload(
    args: argparse.Namespace,
    *,
    server: Any,
    client: Any,
) -> dict[str, Any]:
    if args.scenario_id in {
        "client-initiated-pcm",
        "server-initiated-pcm",
        "server-initiated-pcm-24bit",
        "server-initiated-flac",
        "server-initiated-opus",
        "client-initiated-request-format-pcm",
        "client-initiated-request-format-flac",
    }:
        # The server streams source PCM and the player role re-encodes to whatever
        # format the client negotiates, including a mid-stream stream/request-format
        # switch, so no renegotiation-specific server logic is required here.
        return await _run_audio_scenario(args, server=server, client=client)
    if args.scenario_id in {"client-initiated-metadata", "server-initiated-metadata"}:
        return await _run_metadata_scenario(args, client=client)
    if args.scenario_id in {"client-initiated-controller", "server-initiated-controller"}:
        return await _run_controller_scenario(args, client=client)
    if args.scenario_id in {"client-initiated-artwork", "server-initiated-artwork"}:
        return await _run_artwork_scenario(args, client=client)
    raise ValueError(f"Unsupported scenario for aiosendspin server adapter: {args.scenario_id}")


async def _run(args: argparse.Namespace) -> int:
    _add_repo_to_syspath("aiosendspin")

    from aiosendspin.server.server import SendspinServer

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    summary_path = Path(args.summary)
    ready_path = Path(args.ready)
    registry_path = Path(args.registry)

    loop = asyncio.get_running_loop()
    server = SendspinServer(loop, server_id=args.server_id, server_name=args.server_name)

    try:
        await server.start_server(
            port=args.port,
            host=args.host,
            advertise_addresses=["127.0.0.1"] if args.enable_mdns else [],
            discover_clients=args.enable_mdns,
        )
        server_url = f"ws://127.0.0.1:{args.port}/sendspin"
        if args.initiator_role == "client":
            register_endpoint(
                registry_path,
                args.server_name,
                server_url,
            )
        write_json(
            ready_path,
            {
                "status": "ready",
                "server_id": args.server_id,
                "server_name": args.server_name,
                "scenario_id": args.scenario_id,
                "initiator_role": args.initiator_role,
                "url": server_url,
            },
        )

        if args.initiator_role == "client":
            client, discovery_method = await _wait_for_incoming_client(
                server,
                client_name=args.client_name,
                timeout_s=args.timeout_seconds,
            )
        else:
            client, discovery_method = await _wait_for_target_client(
                server,
                client_name=args.client_name,
                registry_path=registry_path,
                timeout_s=args.timeout_seconds,
            )

        payload = await _scenario_payload(args, server=server, client=client)
        summary = {
            **_base_summary(args, discovery_method=discovery_method, client=client),
            **payload,
        }
        write_json(summary_path, summary)
        print(Path(args.summary).read_text(encoding="utf-8"), end="")
        return 0
    except Exception as err:
        write_json(
            summary_path,
            {
                "status": "error",
                "implementation": "aiosendspin",
                "role": "server",
                "scenario_id": args.scenario_id,
                "reason": str(err),
            },
        )
        return 1
    finally:
        await server.close()


def main() -> int:
    """CLI entrypoint."""
    args = build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
