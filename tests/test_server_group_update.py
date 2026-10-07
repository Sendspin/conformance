"""
Coverage for recording and reporting each server's first ``group/update``.

What matters is the order the two messages went out in, and the local matrix
only ever produces one order: the SDK sends its ``group/update`` after its
``server/activate``. The others are exercised here against stand-ins for the
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
from conformance.site import _render_group_update_section

ACTIVATE = {
    "type": "server/activate",
    "payload": {"activities": ["playback"], "active_roles": ["player@v1"]},
}
STOPPED = {
    "type": "group/update",
    "payload": {"playback_state": "stopped", "group_id": "group-1", "group_name": "Kitchen"},
}
PLAYING = {
    "type": "group/update",
    "payload": {"playback_state": "playing", "group_id": "group-1", "group_name": "Kitchen"},
}


def _transport(socket: object) -> EncryptedWebSocket:
    """Build a transport bound to ``socket`` without running a Noise handshake."""
    transport = object.__new__(EncryptedWebSocket)
    transport._ws = socket
    return transport


def _connection(socket: object) -> Any:
    return SimpleNamespace(_wsock_server=socket, _wsock_client=None, is_encrypted=True)


class SentGroupUpdateRecordingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.refusing = False

        async def send_str(_transport: EncryptedWebSocket, _data: str) -> None:
            if self.refusing:
                raise ConnectionResetError

        patcher = mock.patch.object(EncryptedWebSocket, "send_str", send_str)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.recorder = SentOpeningMessagesRecorder()
        self.addCleanup(self.recorder.uninstall)

    async def _send(self, socket: object, *messages: dict[str, Any]) -> None:
        for message in messages:
            await _transport(socket).send_str(json.dumps(message))

    async def test_reports_the_first_group_update_after_the_activate_as_sent(self) -> None:
        socket = object()
        await self._send(socket, ACTIVATE, {"type": "server/time", "payload": {}}, STOPPED, PLAYING)

        self.assertEqual(self.recorder.first_group_update(_connection(socket)), STOPPED)

    async def test_a_group_update_sent_before_the_activate_is_not_the_one_reported(self) -> None:
        socket = object()
        await self._send(socket, STOPPED, ACTIVATE)

        self.assertIsNone(self.recorder.first_group_update(_connection(socket)))

        await self._send(socket, PLAYING)

        self.assertEqual(self.recorder.first_group_update(_connection(socket)), PLAYING)

    async def test_reports_none_when_no_group_update_followed(self) -> None:
        socket = object()
        await self._send(socket, ACTIVATE, {"type": "server/time", "payload": {}})

        self.assertIsNone(self.recorder.first_group_update(_connection(socket)))

    async def test_reports_none_when_no_activate_was_sent(self) -> None:
        socket = object()
        await self._send(socket, STOPPED)

        self.assertIsNone(self.recorder.first_group_update(_connection(socket)))

    async def test_keeps_connections_apart(self) -> None:
        first, second = object(), object()
        await self._send(first, ACTIVATE, STOPPED)
        await self._send(second, ACTIVATE)

        self.assertEqual(self.recorder.first_group_update(_connection(first)), STOPPED)
        self.assertIsNone(self.recorder.first_group_update(_connection(second)))

    async def test_a_message_that_only_mentions_the_type_is_not_a_group_update(self) -> None:
        socket = object()
        await self._send(
            socket, ACTIVATE, {"type": "server/state", "payload": {"note": "group/update"}}
        )

        self.assertIsNone(self.recorder.first_group_update(_connection(socket)))

    async def test_a_group_update_the_transport_refused_is_not_reported(self) -> None:
        socket = object()
        await self._send(socket, ACTIVATE)
        self.refusing = True
        with self.assertRaises(ConnectionResetError):
            await self._send(socket, STOPPED)

        self.assertIsNone(self.recorder.first_group_update(_connection(socket)))

    async def test_recording_the_group_update_leaves_the_activation_as_sent(self) -> None:
        socket = object()
        await self._send(socket, ACTIVATE, STOPPED)

        self.assertEqual(self.recorder.initial_activation(_connection(socket)), ACTIVATE)


class GroupUpdateSectionTests(unittest.TestCase):
    def test_shows_every_field_the_message_carried(self) -> None:
        section = _render_group_update_section({"group_update": STOPPED}, label="aiosendspin")

        self.assertIn("&quot;stopped&quot;", section)
        self.assertIn("&quot;group-1&quot;", section)
        self.assertIn("&quot;Kitchen&quot;", section)
        self.assertNotIn("observed", section)

    def test_marks_a_required_field_the_message_left_out(self) -> None:
        group_update = {"type": "group/update", "payload": {"playback_state": "playing"}}
        section = _render_group_update_section({"group_update": group_update}, label="aiosendspin")

        self.assertEqual(section.count("not in the message"), 2)

    def test_says_none_followed_an_activate_rather_than_that_none_was_sent(self) -> None:
        section = _render_group_update_section({"group_update": None}, label="sendspin-go")

        self.assertIn("observed after a", section)
        self.assertIn("sendspin-go server sent no", section)
        self.assertIn("never carried a", section)

    def test_summary_without_the_field_renders_no_section(self) -> None:
        self.assertEqual(_render_group_update_section({"status": "error"}, label="aiosendspin"), "")
        self.assertEqual(_render_group_update_section(None, label="aiosendspin"), "")

    def test_escapes_values_taken_from_the_wire(self) -> None:
        group_update = {"type": "group/update", "payload": {"group_name": "<script>"}}
        section = _render_group_update_section({"group_update": group_update}, label="aiosendspin")

        self.assertNotIn("<script>", section)
        self.assertIn("&lt;script&gt;", section)

    def test_shows_a_malformed_recording_whole(self) -> None:
        section = _render_group_update_section({"group_update": "garbled"}, label="aiosendspin")

        self.assertIn("Recorded value", section)
        self.assertIn("&quot;garbled&quot;", section)


if __name__ == "__main__":
    unittest.main()
