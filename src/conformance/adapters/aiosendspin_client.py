"""aiosendspin client adapter for conformance scenarios."""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import sys
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from conformance.flac import StreamingFlacDecoder
from conformance.io import write_json
from conformance.pcm import FloatPcmHasher, sha256_hex
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
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--scenario-id", default="server-initiated-flac")
    parser.add_argument("--initiator-role", choices=("server", "client"), default="server")
    parser.add_argument("--preferred-codec", default="flac")
    parser.add_argument("--server-name", default="Sendspin Conformance Server")
    parser.add_argument("--server-id", default="conformance-server")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--port", type=int, default=8928)
    parser.add_argument("--path", default="/sendspin")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--enable-mdns", action="store_true")
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
    # Accepted for CLI symmetry with the server; the client observes the
    # advertised repeat/shuffle from the controller state rather than these.
    parser.add_argument("--controller-repeat", default="all")
    parser.add_argument("--controller-shuffle", default="false")
    parser.add_argument("--artwork-format", default="jpeg")
    parser.add_argument("--artwork-width", type=int, default=256)
    parser.add_argument("--artwork-height", type=int, default=256)
    return parser


def _supported_formats(preferred_codec: str, *, scenario_id: str = "") -> list[Any]:
    from aiosendspin.models.player import SupportedAudioFormat
    from aiosendspin.models.types import AudioCodec

    codec_lookup = {
        "pcm": AudioCodec.PCM,
        "flac": AudioCodec.FLAC,
        "opus": AudioCodec.OPUS,
    }
    codec = codec_lookup.get(preferred_codec)
    if codec is None:
        raise ValueError(f"Unsupported preferred codec: {preferred_codec}")
    if preferred_codec == "opus":
        return [
            SupportedAudioFormat(
                codec=codec,
                channels=1,
                sample_rate=48_000,
                bit_depth=16,
            ),
            SupportedAudioFormat(
                codec=codec,
                channels=2,
                sample_rate=48_000,
                bit_depth=16,
            ),
        ]
    if scenario_id == "server-initiated-pcm-24bit":
        # Match the fixture's native rate/channels so the SDK does not resample
        # during the round trip; only the bit depth changes, which preserves
        # the float-domain PCM hash through the 16→32→24 conversion.
        return [
            SupportedAudioFormat(
                codec=codec,
                channels=1,
                sample_rate=8_000,
                bit_depth=24,
            ),
        ]
    return [
        SupportedAudioFormat(
            codec=codec,
            channels=1,
            sample_rate=8_000,
            bit_depth=16,
        ),
        SupportedAudioFormat(
            codec=codec,
            channels=2,
            sample_rate=44_100,
            bit_depth=16,
        ),
        SupportedAudioFormat(
            codec=codec,
            channels=2,
            sample_rate=48_000,
            bit_depth=16,
        ),
    ]


async def _wait_for_server_url(registry_path: Path, server_name: str, timeout_s: float) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        url = lookup_endpoint(registry_path, server_name)
        if url is not None:
            return url
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for server {server_name!r}")


def _normalize_metadata_state(metadata: Any) -> dict[str, Any] | None:
    if metadata is None:
        return None
    progress = None
    if getattr(metadata, "progress", None) is not None:
        progress = {
            "track_progress": metadata.progress.track_progress,
            "track_duration": metadata.progress.track_duration,
            "playback_speed": metadata.progress.playback_speed,
        }
    return {
        "title": getattr(metadata, "title", None),
        "artist": getattr(metadata, "artist", None),
        "album_artist": getattr(metadata, "album_artist", None),
        "album": getattr(metadata, "album", None),
        "artwork_url": getattr(metadata, "artwork_url", None),
        "year": getattr(metadata, "year", None),
        "track": getattr(metadata, "track", None),
        "progress": progress,
    }


def _normalize_controller_state(controller: Any) -> dict[str, Any] | None:
    if controller is None:
        return None
    repeat = getattr(controller, "repeat", None)
    return {
        "supported_commands": [command.value for command in controller.supported_commands],
        "volume": controller.volume,
        "muted": controller.muted,
        "repeat": None if repeat is None else repeat.value,
        "shuffle": getattr(controller, "shuffle", None),
    }


async def _run(args: argparse.Namespace) -> int:
    _add_repo_to_syspath("aiosendspin")

    from aiosendspin.client import ClientListener, SendspinClient
    from aiosendspin.models.artwork import ArtworkChannel, ClientHelloArtworkSupport
    from aiosendspin.models.player import ClientHelloPlayerSupport
    from aiosendspin.models.types import (
        MediaCommand,
        PictureFormat,
        Roles,
        ArtworkSource,
    )
    from aiosendspin.models.types import PlayerCommand

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    summary_path = Path(args.summary)
    ready_path = Path(args.ready)
    registry_path = Path(args.registry)
    disconnect_event = asyncio.Event()
    received_server_hello: dict[str, Any] | None = None

    audio_state: dict[str, Any] = {
        "chunk_count": 0,
        "stream": None,
    }
    received_hasher = FloatPcmHasher()
    encoded_accumulator = bytearray()
    current_decoder: StreamingFlacDecoder | None = None

    metadata_state: dict[str, Any] = {
        "received": None,
        "update_count": 0,
    }
    controller_state: dict[str, Any] = {
        "received_state": None,
        "sent_command": None,
    }
    artwork_state: dict[str, Any] = {
        "stream": None,
        "channel": None,
        "received_count": 0,
        "received_sha256": None,
        "byte_count": 0,
    }
    artwork_hasher = sha256()

    def flush_decoder() -> None:
        nonlocal current_decoder
        if current_decoder is None:
            return
        try:
            pcm = current_decoder.flush()
        except Exception:
            current_decoder = None
            return
        current_decoder = None
        if pcm:
            received_hasher.update_from_pcm_bytes(pcm, bit_depth=16)

    def record_disconnect() -> None:
        flush_decoder()
        disconnect_event.set()

    def on_stream_start(message: Any) -> None:
        nonlocal current_decoder
        if message.payload.artwork is not None:
            artwork_state["stream"] = {
                "channels": [
                    {
                        "source": channel.source.value,
                        "format": channel.format.value,
                        "width": channel.width,
                        "height": channel.height,
                    }
                    for channel in message.payload.artwork.channels
                ]
            }
        player = message.payload.player
        if player is None:
            return
        codec_header = None
        if player.codec_header:
            codec_header = base64.b64decode(player.codec_header)
        audio_state["stream"] = {
            "codec": player.codec.value,
            "sample_rate": player.sample_rate,
            "channels": player.channels,
            "bit_depth": player.bit_depth,
            "codec_header": player.codec_header,
        }
        if player.codec.value == "flac":
            current_decoder = StreamingFlacDecoder(
                sample_rate=player.sample_rate,
                channels=player.channels,
                bit_depth=player.bit_depth,
                codec_header=codec_header,
            )
        else:
            current_decoder = None

    def on_audio_chunk(timestamp_us: int, payload: bytes, audio_format: Any) -> None:
        del timestamp_us
        audio_state["chunk_count"] += 1
        encoded_accumulator.extend(payload)
        codec = audio_format.codec.value
        if codec == "flac":
            if current_decoder is None:
                raise RuntimeError("Received FLAC before stream/start")
            pcm = current_decoder.decode(payload)
            received_hasher.update_from_pcm_bytes(pcm, bit_depth=audio_format.pcm_format.bit_depth)
            return
        if codec == "pcm":
            received_hasher.update_from_pcm_bytes(payload, bit_depth=audio_format.pcm_format.bit_depth)
            return
        if codec == "opus":
            return
        raise RuntimeError(f"Unsupported codec for this adapter: {codec}")

    def on_stream_end(_roles: list[str] | None) -> None:
        flush_decoder()

    def on_server_hello(payload: Any) -> None:
        nonlocal received_server_hello
        received_server_hello = {
            "type": "server/hello",
            "payload": payload.to_dict(),
        }

    def on_artwork_chunk(channel: int, data: bytes) -> None:
        artwork_state["channel"] = channel
        artwork_state["received_count"] += 1
        artwork_state["byte_count"] += len(data)
        artwork_hasher.update(data)
        artwork_state["received_sha256"] = artwork_hasher.copy().hexdigest()

    scenario_roles: list[Any]
    artwork_support: Any | None = None
    player_support: Any | None = None

    if args.scenario_id in {
        "client-initiated-pcm",
        "server-initiated-pcm",
        "server-initiated-pcm-24bit",
        "server-initiated-flac",
        "server-initiated-opus",
    }:
        player_support = ClientHelloPlayerSupport(
            supported_formats=_supported_formats(
                args.preferred_codec, scenario_id=args.scenario_id
            ),
            buffer_capacity=2_000_000,
            supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
        )
        scenario_roles = [Roles.PLAYER]
    elif args.scenario_id in {"client-initiated-metadata", "server-initiated-metadata"}:
        scenario_roles = [Roles.METADATA]
    elif args.scenario_id in {"client-initiated-controller", "server-initiated-controller"}:
        scenario_roles = [Roles.CONTROLLER]
    elif args.scenario_id in {"client-initiated-artwork", "server-initiated-artwork"}:
        scenario_roles = [Roles.ARTWORK]
        artwork_support = ClientHelloArtworkSupport(
            channels=[
                ArtworkChannel(
                    source=ArtworkSource.ALBUM,
                    format=PictureFormat(args.artwork_format.lower()),
                    media_width=args.artwork_width,
                    media_height=args.artwork_height,
                )
            ]
        )
    else:
        write_json(
            summary_path,
            {
                "status": "error",
                "reason": f"Unsupported scenario for aiosendspin client adapter: {args.scenario_id}",
            },
        )
        return 1

    client = SendspinClient(
        client_id=args.client_id,
        client_name=args.client_name,
        roles=scenario_roles,
        player_support=player_support,
        artwork_support=artwork_support,
    )

    client.add_server_hello_listener(on_server_hello)
    client.add_stream_start_listener(on_stream_start)
    client.add_artwork_listener(on_artwork_chunk)

    if args.scenario_id in {
        "client-initiated-pcm",
        "server-initiated-pcm",
        "server-initiated-pcm-24bit",
        "server-initiated-flac",
        "server-initiated-opus",
    }:
        client.add_audio_chunk_listener(on_audio_chunk)
        client.add_stream_end_listener(on_stream_end)

    if args.scenario_id in {"client-initiated-metadata", "server-initiated-metadata"}:
        def on_metadata(payload: Any) -> None:
            metadata_state["update_count"] += 1
            metadata_state["received"] = _normalize_metadata_state(payload.metadata)

        client.add_metadata_listener(on_metadata)

    if args.scenario_id in {"client-initiated-controller", "server-initiated-controller"}:
        async def send_command() -> None:
            command = MediaCommand(args.controller_command)
            await client.send_group_command(command)

        def on_controller_state(payload: Any) -> None:
            controller = getattr(payload, "controller", None)
            if controller is None:
                return
            normalized = _normalize_controller_state(controller)
            controller_state["received_state"] = normalized
            supported = set(normalized["supported_commands"])
            if controller_state["sent_command"] is None and args.controller_command in supported:
                controller_state["sent_command"] = {"command": args.controller_command}
                asyncio.create_task(send_command())

        client.add_controller_state_listener(on_controller_state)

    client.add_disconnect_listener(record_disconnect)

    async def handle_connection(ws: Any) -> None:
        await client.attach_websocket(ws)
        await disconnect_event.wait()

    try:
        if args.initiator_role == "client":
            write_json(
                ready_path,
                {
                    "status": "ready",
                    "scenario_id": args.scenario_id,
                    "initiator_role": args.initiator_role,
                },
            )
            target_url = await _wait_for_server_url(
                registry_path,
                args.server_name,
                args.timeout_seconds,
            )
            await client.connect(target_url)
            await asyncio.wait_for(disconnect_event.wait(), timeout=args.timeout_seconds)
        else:
            listener = ClientListener(
                client_id=args.client_id,
                client_name=args.client_name,
                on_connection=handle_connection,
                port=args.port,
                path=args.path,
                advertise_mdns=args.enable_mdns,
            )
            await listener.start()
            try:
                register_endpoint(
                    registry_path,
                    args.client_name,
                    f"ws://127.0.0.1:{args.port}{args.path}",
                )
                write_json(
                    ready_path,
                    {
                        "status": "ready",
                        "scenario_id": args.scenario_id,
                        "initiator_role": args.initiator_role,
                        "url": f"ws://127.0.0.1:{args.port}{args.path}",
                    },
                )
                await asyncio.wait_for(disconnect_event.wait(), timeout=args.timeout_seconds)
            finally:
                await listener.stop()
    except TimeoutError:
        write_json(
            summary_path,
            {
                "status": "error",
                "reason": "Timed out waiting for server disconnect",
            },
        )
        return 1
    except Exception as err:
        write_json(
            summary_path,
            {
                "status": "error",
                "reason": str(err),
            },
        )
        return 1

    summary: dict[str, Any] = {
        "status": "ok",
        "implementation": "aiosendspin",
        "role": "client",
        "client_name": args.client_name,
        "client_id": args.client_id,
        "scenario_id": args.scenario_id,
        "initiator_role": args.initiator_role,
        "preferred_codec": args.preferred_codec,
        "peer_hello": received_server_hello,
        "server": asdict(client.server_info) if client.server_info is not None else None,
    }

    if args.scenario_id in {
        "client-initiated-pcm",
        "server-initiated-pcm",
        "server-initiated-pcm-24bit",
        "server-initiated-flac",
        "server-initiated-opus",
    }:
        summary["stream"] = audio_state["stream"]
        summary["audio"] = {
            "audio_chunk_count": audio_state["chunk_count"],
            "received_encoded_sha256": sha256_hex(bytes(encoded_accumulator)),
            "received_pcm_sha256": received_hasher.hexdigest(),
            "received_sample_count": received_hasher.sample_count,
        }
    elif args.scenario_id in {"client-initiated-metadata", "server-initiated-metadata"}:
        summary["metadata"] = {
            "update_count": metadata_state["update_count"],
            "received": metadata_state["received"],
        }
    elif args.scenario_id in {"client-initiated-controller", "server-initiated-controller"}:
        summary["controller"] = {
            "received_state": controller_state["received_state"],
            "sent_command": controller_state["sent_command"],
        }
    elif args.scenario_id in {"client-initiated-artwork", "server-initiated-artwork"}:
        summary["stream"] = artwork_state["stream"]
        summary["artwork"] = {
            "channel": artwork_state["channel"],
            "received_count": artwork_state["received_count"],
            "received_sha256": artwork_state["received_sha256"],
            "byte_count": artwork_state["byte_count"],
        }

    write_json(summary_path, summary)
    print(Path(args.summary).read_text(encoding="utf-8"), end="")
    return 0


def main() -> int:
    """CLI entrypoint."""
    args = build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
