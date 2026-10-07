"""
Coverage for recording and reporting each server's initial ``server/activate``.

The two states that matter most are the ones the local matrix cannot produce on
demand: a server that sends no ``server/activate`` at all, and a recorder that
has stopped observing the SDK. Both are exercised here against stand-ins for the
SDK transport and connection.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from aiosendspin.noise.wire import EncryptedWebSocket

from conformance.adapters._aiosendspin_protocol_evidence import SentOpeningMessagesRecorder
from conformance.site import _render_activation_section

PLAYBACK_ACTIVATE = {
    "type": "server/activate",
    "payload": {"activities": ["playback"], "active_roles": ["player@v1"]},
}
IDLE_ACTIVATE = {"type": "server/activate", "payload": {"activities": []}}


def _transport(socket: object) -> EncryptedWebSocket:
    """Build a transport bound to ``socket`` without running a Noise handshake."""
    transport = object.__new__(EncryptedWebSocket)
    transport._ws = socket
    return transport


def _connection(socket: object, *, encrypted: bool = True) -> Any:
    return SimpleNamespace(_wsock_server=socket, _wsock_client=None, is_encrypted=encrypted)


class SentOpeningMessagesRecorderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sent: list[str] = []

        async def send_str(_transport: EncryptedWebSocket, data: str) -> None:
            self.sent.append(data)

        patcher = mock.patch.object(EncryptedWebSocket, "send_str", send_str)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.recorder = SentOpeningMessagesRecorder()
        self.addCleanup(self.recorder.uninstall)

    async def test_reports_the_first_activate_exactly_as_sent(self) -> None:
        socket = object()
        transport = _transport(socket)
        await transport.send_str(json.dumps({"type": "server/time", "payload": {}}))
        await transport.send_str(json.dumps(IDLE_ACTIVATE))
        await transport.send_str(json.dumps(PLAYBACK_ACTIVATE))

        self.assertEqual(self.recorder.initial_activation(_connection(socket)), IDLE_ACTIVATE)
        self.assertEqual(len(self.sent), 3)

    async def test_keeps_connections_apart(self) -> None:
        first, second = object(), object()
        await _transport(first).send_str(json.dumps(IDLE_ACTIVATE))
        await _transport(second).send_str(json.dumps(PLAYBACK_ACTIVATE))

        self.assertEqual(self.recorder.initial_activation(_connection(first)), IDLE_ACTIVATE)
        self.assertEqual(self.recorder.initial_activation(_connection(second)), PLAYBACK_ACTIVATE)

    async def test_a_message_that_only_mentions_the_type_is_not_an_activate(self) -> None:
        socket = object()
        await _transport(socket).send_str(
            json.dumps({"type": "server/state", "payload": {"note": "server/activate"}})
        )

        self.assertIsNone(self.recorder.initial_activation(_connection(socket, encrypted=False)))

    async def test_a_message_that_failed_to_send_is_not_recorded(self) -> None:
        async def failing_send_str(_transport: EncryptedWebSocket, _data: str) -> None:
            raise ConnectionResetError

        socket = object()
        with mock.patch.object(self.recorder, "_original_send_str", failing_send_str):
            with self.assertRaises(ConnectionResetError):
                await _transport(socket).send_str(json.dumps(PLAYBACK_ACTIVATE))

        self.assertIsNone(self.recorder.initial_activation(_connection(socket, encrypted=False)))

    def test_unencrypted_connection_reports_no_activation(self) -> None:
        self.assertIsNone(self.recorder.initial_activation(_connection(object(), encrypted=False)))

    def test_encrypted_connection_without_a_recording_is_an_error(self) -> None:
        with self.assertRaises(RuntimeError):
            self.recorder.initial_activation(_connection(object()))

    async def test_uninstall_stops_recording(self) -> None:
        socket = object()
        self.recorder.uninstall()
        await _transport(socket).send_str(json.dumps(PLAYBACK_ACTIVATE))

        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(self.recorder.initial_activation(_connection(socket, encrypted=False)))


class ActivationSectionTests(unittest.TestCase):
    def test_shows_declared_activities_and_active_roles(self) -> None:
        section = _render_activation_section({"activation": PLAYBACK_ACTIVATE}, label="aiosendspin")

        self.assertIn(">playback<", section)
        self.assertIn(">player@v1<", section)
        self.assertNotIn("observed", section)

    def test_distinguishes_an_empty_list_from_a_missing_field(self) -> None:
        section = _render_activation_section({"activation": IDLE_ACTIVATE}, label="aiosendspin")

        self.assertIn("empty list", section)
        self.assertIn("not in the message", section)

    def test_shows_fields_the_spec_does_not_define(self) -> None:
        activation = {"type": "server/activate", "payload": {"activities": [], "extra": {"a": 1}}}
        section = _render_activation_section({"activation": activation}, label="aiosendspin")

        self.assertIn(">extra<", section)
        self.assertIn("{&quot;a&quot;: 1}", section)

    def test_states_plainly_when_no_activate_was_sent(self) -> None:
        section = _render_activation_section({"activation": None}, label="sendspin-go")

        self.assertIn("No <span class='font-mono text-[13px]'>server/activate</span> observed", section)
        self.assertIn("sendspin-go server sent no", section)

    def test_summary_without_the_field_renders_no_section(self) -> None:
        self.assertEqual(_render_activation_section({"status": "error"}, label="aiosendspin"), "")
        self.assertEqual(_render_activation_section(None, label="aiosendspin"), "")

    def test_escapes_values_taken_from_the_wire(self) -> None:
        activation = {"type": "server/activate", "payload": {"activities": ["<script>"]}}
        section = _render_activation_section({"activation": activation}, label="aiosendspin")

        self.assertNotIn("<script>", section)
        self.assertIn("&lt;script&gt;", section)

    def test_shows_a_malformed_recording_whole(self) -> None:
        section = _render_activation_section({"activation": "garbled"}, label="aiosendspin")

        self.assertIn("Recorded value", section)
        self.assertIn("&quot;garbled&quot;", section)

    def test_shows_a_recording_of_another_message_whole(self) -> None:
        recorded = {"type": "server/hello", "payload": PLAYBACK_ACTIVATE["payload"]}
        section = _render_activation_section({"activation": recorded}, label="aiosendspin")

        self.assertIn("Recorded value", section)
        self.assertIn("server/hello", section)
        self.assertNotIn(">activities<", section)


if __name__ == "__main__":
    unittest.main()
