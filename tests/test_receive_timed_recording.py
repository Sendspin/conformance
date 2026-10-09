"""
Messages the SDK reads through ``receive_timed`` reach the recorders.

``EncryptedWebSocket.receive_timed`` returns a decrypted message together with
the time its last frame arrived, and does not go through ``receive``. The SDK
reads every encrypted connection that way, so a recorder that observes only
``receive`` sees nothing a connection receives: no ``client/state``, no
``client/time`` and no binary frame.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from aiohttp import WSMsgType
from aiosendspin.noise.wire import EncryptedWebSocket

from conformance.adapters._aiosendspin_protocol_evidence import (
    ControlMessageRecorder,
    ReceivedBinaryFrameRecorder,
    binary_frame_record,
)

RECEIVED_AT_US = 1_234_567


def _transport(socket: object) -> EncryptedWebSocket:
    """Build a transport bound to ``socket`` without running a Noise handshake."""
    transport = object.__new__(EncryptedWebSocket)
    transport._ws = socket
    return transport


def _connection(socket: object) -> Any:
    return SimpleNamespace(_wsock_server=socket, _wsock_client=None, is_encrypted=True)


def _message(message_type: str, **payload: Any) -> str:
    return json.dumps({"type": message_type, "payload": payload})


class _TimedReceiveCase(unittest.IsolatedAsyncioTestCase):
    """Stand in for the SDK's ``receive_timed`` before a recorder wraps it."""

    def setUp(self) -> None:
        self.incoming: list[Any] = []

        async def receive_timed(_transport: EncryptedWebSocket, _clock: Any) -> Any:
            return self.incoming.pop(0), RECEIVED_AT_US

        self.stand_in = receive_timed
        patcher = mock.patch.object(EncryptedWebSocket, "receive_timed", receive_timed, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _receive_timed(
        self, transport: EncryptedWebSocket, data: Any, kind: Any = WSMsgType.TEXT
    ) -> Any:
        message = SimpleNamespace(type=kind, data=data)
        self.incoming.append(message)
        returned = await transport.receive_timed(object())
        # The caller gets back exactly what the SDK's own method returned.
        self.assertEqual(returned, (message, RECEIVED_AT_US))
        return returned


class ControlMessageRecorderTimedReceiveTests(_TimedReceiveCase):
    def setUp(self) -> None:
        super().setUp()
        self.recorder = ControlMessageRecorder()
        self.addCleanup(self.recorder.uninstall)

    async def test_records_a_client_state_read_through_receive_timed(self) -> None:
        socket = object()
        await self._receive_timed(_transport(socket), _message("client/state", available=True))

        self.assertEqual(
            self.recorder.availability_trace(_connection(socket)),
            [{"type": "client/state", "available": True}],
        )

    async def test_records_a_client_time_read_through_receive_timed(self) -> None:
        socket = object()
        client_time = _message("client/time", client_transmitted=42)
        await self._receive_timed(_transport(socket), client_time)

        self.assertEqual(
            self.recorder.time_exchange(_connection(socket)), [json.loads(client_time)]
        )

    async def test_uninstall_restores_the_sdks_own_receive_timed(self) -> None:
        self.assertIsNot(EncryptedWebSocket.receive_timed, self.stand_in)

        self.recorder.uninstall()

        self.assertIs(EncryptedWebSocket.receive_timed, self.stand_in)


class ReceivedBinaryFrameRecorderTimedReceiveTests(_TimedReceiveCase):
    def setUp(self) -> None:
        super().setUp()
        self.recorder = ReceivedBinaryFrameRecorder()
        self.addCleanup(self.recorder.uninstall)

    async def test_records_a_binary_frame_read_through_receive_timed(self) -> None:
        frame = bytes([4]) + bytes(12) + b"audio"
        await self._receive_timed(_transport(object()), frame, WSMsgType.BINARY)

        self.assertEqual(self.recorder.frames(), [binary_frame_record(frame)])

    async def test_uninstall_restores_the_sdks_own_receive_timed(self) -> None:
        self.assertIsNot(EncryptedWebSocket.receive_timed, self.stand_in)

        self.recorder.uninstall()

        self.assertIs(EncryptedWebSocket.receive_timed, self.stand_in)


class SdkWithoutReceiveTimedTests(unittest.TestCase):
    """An SDK that predates ``receive_timed`` is recorded through ``receive`` as before."""

    def test_recorders_install_and_uninstall_without_it(self) -> None:
        original = EncryptedWebSocket.__dict__.get("receive_timed")
        if original is not None:
            delattr(EncryptedWebSocket, "receive_timed")
            self.addCleanup(setattr, EncryptedWebSocket, "receive_timed", original)

        for recorder_class in (ControlMessageRecorder, ReceivedBinaryFrameRecorder):
            with self.subTest(recorder_class.__name__):
                recorder = recorder_class()
                recorder.uninstall()
                self.assertFalse(hasattr(EncryptedWebSocket, "receive_timed"))


if __name__ == "__main__":
    unittest.main()
