"""The bundled aiosendspin server reports every frame of a connection to its frame observer."""

import asyncio
import json
import socket
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from conformance._vendor.aiosendspin.client import ClientListener, SendspinClient
from conformance._vendor.aiosendspin.models.player import (
    ClientHelloPlayerSupport,
    SupportedAudioFormat,
)
from conformance._vendor.aiosendspin.models.types import AudioCodec, Roles
from conformance._vendor.aiosendspin.noise import (
    ClientPairingRecord,
    Identity,
    InMemoryClientPairingStore,
    InMemoryServerPairingStore,
    PairingPsk,
    PskCategory,
    ServerPairingRecord,
    StagedPairingPsk,
    generate_psk,
    psk_id_for,
)
from conformance._vendor.aiosendspin.server import (
    FrameDirection,
    FramePhase,
    ObservedFrame,
    SendspinServer,
)
from conformance._vendor.aiosendspin.server.audio import AudioFormat

IN = FrameDirection.IN
OUT = FrameDirection.OUT
CLEARTEXT = FramePhase.CLEARTEXT
TRANSPORT = FramePhase.TRANSPORT
AUDIO_CHUNK = "binary 4"
TIMEOUT_S = 10.0

# The opening of every encrypted connection, in the order the frames travel.
CONNECTION_OPENING = [
    (IN, CLEARTEXT, "client/init"),
    (OUT, CLEARTEXT, "server/init"),
    (OUT, CLEARTEXT, "noise/handshake"),
    (IN, CLEARTEXT, "noise/handshake"),
    (OUT, TRANSPORT, "server/hello"),
    (IN, TRANSPORT, "client/hello"),
    (OUT, TRANSPORT, "server/activate"),
]


def frame_label(frame: ObservedFrame) -> tuple[FrameDirection, FramePhase, str]:
    """Return a frame's direction, phase, and its message type, binary type byte or error."""
    if frame.error is not None:
        return frame.direction, frame.phase, f"error: {frame.error}"
    if frame.phase is CLEARTEXT:
        return frame.direction, frame.phase, json.loads(frame.data)["type"]
    if frame.data[0] == 0:
        return frame.direction, frame.phase, json.loads(frame.data[1:])["type"]
    return frame.direction, frame.phase, f"binary {frame.data[0]}"


class FrameRecorder:
    """Frame observer that keeps every frame it is handed."""

    def __init__(self) -> None:
        self.frames: list[ObservedFrame] = []

    def __call__(self, frame: ObservedFrame) -> None:
        self.frames.append(frame)

    def labels(self) -> list[tuple[FrameDirection, FramePhase, str]]:
        return [frame_label(frame) for frame in self.frames]

    def index(self, label: tuple[FrameDirection, FramePhase, str], start: int = 0) -> int:
        """Return the position of the first frame with `label` from `start`, or -1."""
        labels = self.labels()
        return next((i for i in range(start, len(labels)) if labels[i] == label), -1)

    async def wait_for(self, label: tuple[FrameDirection, FramePhase, str]) -> None:
        async with asyncio.timeout(TIMEOUT_S):
            while self.index(label) < 0:
                await asyncio.sleep(0.02)


def player_client(identity: Identity, store: InMemoryClientPairingStore) -> SendspinClient:
    return SendspinClient(
        identity=identity,
        client_name="frame-observer-client",
        roles=[Roles.PLAYER],
        pairing_store=store,
        player_support=ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(
                    codec=AudioCodec.PCM, channels=2, sample_rate=48_000, bit_depth=16
                )
            ],
            buffer_capacity=2_000_000,
        ),
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FrameObserverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.recorder = FrameRecorder()
        self.server_store = InMemoryServerPairingStore()
        self.client_store = InMemoryClientPairingStore()
        self.client_identity = Identity.generate()
        self.server = SendspinServer(
            loop=asyncio.get_running_loop(),
            identity=Identity.generate(),
            server_name="frame-observer-server",
            pairing_store=self.server_store,
            allow_noncompliant_clients=False,
            frame_observer=self.recorder,
        )
        self.addAsyncCleanup(self.server.close)

    async def test_client_initiated_connection_is_observed_in_order(self) -> None:
        await self._store_long_term_records()
        url = await self._serve()
        client = player_client(self.client_identity, self.client_store)
        self.addAsyncCleanup(client.disconnect)

        await client.connect(url)
        await self._stream_audio()

        self._assert_player_session_observed()

    async def test_server_initiated_connection_is_observed_in_order(self) -> None:
        await self._store_long_term_records()
        client = player_client(self.client_identity, self.client_store)
        port = free_port()

        listener = ClientListener(
            client_id=self.client_identity.peer_id,
            on_connection=client.attach_websocket,
            port=port,
            host="127.0.0.1",
            advertise_mdns=False,
        )
        await listener.start()
        self.addAsyncCleanup(listener.stop)
        self.addAsyncCleanup(client.disconnect)

        await self.server.connect_to_client_and_wait(f"ws://127.0.0.1:{port}/sendspin")
        await self._stream_audio()

        self._assert_player_session_observed()

    async def test_pairing_psk_pairing_is_observed(self) -> None:
        pairing_psk = generate_psk()
        psk_id = psk_id_for(pairing_psk)
        await self.client_store.set_pairing_psk(PairingPsk(psk_id=psk_id, psk=pairing_psk))
        await self.server_store.stage_pairing_psk(
            self.client_identity.peer_id, StagedPairingPsk(psk_id=psk_id, psk=pairing_psk)
        )
        url = await self._serve()
        client = SendspinClient(
            identity=self.client_identity,
            client_name="frame-observer-client",
            roles=[Roles.CONTROLLER],
            pairing_store=self.client_store,
        )
        self.addAsyncCleanup(client.disconnect)

        await client.connect(url)
        async with asyncio.timeout(TIMEOUT_S):
            while await self.client_store.record_by_server_id(self.server.id) is None:
                await asyncio.sleep(0.02)
        self.assertIsNotNone(client.noise_psk)
        self.assertIs(client.noise_psk.category, PskCategory.LONG_TERM)

        labels = self.recorder.labels()
        self.assertEqual(labels[: len(CONNECTION_OPENING)], CONNECTION_OPENING)
        pairing = [label for label in labels if "/pair-" in label[2]]
        self.assertEqual(
            pairing,
            [
                (IN, TRANSPORT, "client/pair-init"),
                (IN, TRANSPORT, "client/pair-finalize"),
                (OUT, TRANSPORT, "server/pair-finalize"),
            ],
        )
        # Pairing ends in an in-band re-handshake onto the new long-term PSK.
        finalized = labels.index((OUT, TRANSPORT, "server/pair-finalize"))
        self.assertEqual(
            [label for label in labels[finalized:] if label[2] == "noise/handshake"],
            [(OUT, TRANSPORT, "noise/handshake"), (IN, TRANSPORT, "noise/handshake")],
        )
        self.assertEqual({frame.connection for frame in self.recorder.frames}, {1})
        self.assertTrue(all(frame.error is None for frame in self.recorder.frames))

    async def _store_long_term_records(self) -> None:
        psk = generate_psk()
        psk_id = psk_id_for(psk)
        await self.server_store.store_record(
            ServerPairingRecord(
                psk_id=psk_id, psk=psk, client_id=self.client_identity.peer_id, pair_methods=[]
            )
        )
        await self.client_store.store_record(
            ClientPairingRecord(psk_id=psk_id, psk=psk, server_id=self.server.id)
        )

    async def _serve(self) -> str:
        """
        Accept client connections on localhost and return the server's URL.

        Register a client's disconnect after this, so it runs before the test server closes.
        """
        app = web.Application()
        app.router.add_get(SendspinServer.API_PATH, self.server.on_client_connect)
        test_server = TestServer(app, host="127.0.0.1")
        await test_server.start_server()
        self.addAsyncCleanup(test_server.close)
        return f"ws://127.0.0.1:{test_server.port}{SendspinServer.API_PATH}"

    async def _stream_audio(self) -> None:
        """Stream half a second of silence to the client once it reports available."""
        async with asyncio.timeout(TIMEOUT_S):
            while (
                client := self.server.get_client(self.client_identity.peer_id)
            ) is None or not client.available:
                await asyncio.sleep(0.02)
        stream = client.group.start_stream()
        stream.prepare_audio(bytes(48_000 * 2 * 2 // 2), AudioFormat(48_000, 16, 2))
        await stream.commit_audio(play_start_us=self.server.clock.now_us() + 250_000)
        await self.recorder.wait_for((OUT, TRANSPORT, AUDIO_CHUNK))
        await self.recorder.wait_for((OUT, TRANSPORT, "server/time"))

    def _assert_player_session_observed(self) -> None:
        labels = self.recorder.labels()
        self.assertEqual(labels[: len(CONNECTION_OPENING)], CONNECTION_OPENING)
        client_time = self.recorder.index((IN, TRANSPORT, "client/time"), len(CONNECTION_OPENING))
        self.assertGreater(client_time, 0)
        self.assertGreater(self.recorder.index((OUT, TRANSPORT, "server/time"), client_time), 0)
        self.assertIn((OUT, TRANSPORT, AUDIO_CHUNK), labels)

        self.assertEqual({frame.connection for frame in self.recorder.frames}, {1})
        # The handshake names the client, so only its four cleartext frames precede that.
        self.assertEqual(
            {frame.client_id for frame in self.recorder.frames[4:]}, {self.client_identity.peer_id}
        )
        self.assertTrue(all(frame.error is None for frame in self.recorder.frames))
        times = [frame.monotonic_us for frame in self.recorder.frames]
        self.assertEqual(times, sorted(times))


if __name__ == "__main__":
    unittest.main()
