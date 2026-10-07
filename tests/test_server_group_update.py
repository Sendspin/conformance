"""
Coverage for recording and reporting each server's ``group/update`` messages.

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

from conformance.adapters._aiosendspin_protocol_evidence import ControlMessageRecorder
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
        self.recorder = ControlMessageRecorder()
        self.addCleanup(self.recorder.uninstall)

    async def _send(self, socket: object, *messages: dict[str, Any]) -> None:
        for message in messages:
            await _transport(socket).send_str(json.dumps(message))

    async def test_reports_every_group_update_after_the_activate_in_order(self) -> None:
        socket = object()
        await self._send(socket, ACTIVATE, {"type": "server/time", "payload": {}}, STOPPED, PLAYING)

        self.assertEqual(self.recorder.group_updates(_connection(socket)), [STOPPED, PLAYING])

    async def test_a_group_update_sent_before_the_activate_is_left_out(self) -> None:
        socket = object()
        await self._send(socket, STOPPED, ACTIVATE)

        self.assertEqual(self.recorder.group_updates(_connection(socket)), [])

        await self._send(socket, PLAYING)

        self.assertEqual(self.recorder.group_updates(_connection(socket)), [PLAYING])

    async def test_a_later_activate_does_not_restart_the_record(self) -> None:
        socket = object()
        await self._send(socket, ACTIVATE, STOPPED, ACTIVATE, PLAYING)

        self.assertEqual(self.recorder.group_updates(_connection(socket)), [STOPPED, PLAYING])

    async def test_reports_none_when_no_group_update_followed(self) -> None:
        socket = object()
        await self._send(socket, ACTIVATE, {"type": "server/time", "payload": {}})

        self.assertEqual(self.recorder.group_updates(_connection(socket)), [])

    async def test_reports_none_when_no_activate_was_sent(self) -> None:
        socket = object()
        await self._send(socket, STOPPED)

        self.assertEqual(self.recorder.group_updates(_connection(socket)), [])

    async def test_keeps_connections_apart(self) -> None:
        first, second = object(), object()
        await self._send(first, ACTIVATE, STOPPED)
        await self._send(second, ACTIVATE)

        self.assertEqual(self.recorder.group_updates(_connection(first)), [STOPPED])
        self.assertEqual(self.recorder.group_updates(_connection(second)), [])

    async def test_a_message_that_only_mentions_the_type_is_not_a_group_update(self) -> None:
        socket = object()
        await self._send(
            socket, ACTIVATE, {"type": "server/state", "payload": {"note": "group/update"}}
        )

        self.assertEqual(self.recorder.group_updates(_connection(socket)), [])

    async def test_a_group_update_the_transport_refused_is_not_reported(self) -> None:
        socket = object()
        await self._send(socket, ACTIVATE, STOPPED)
        self.refusing = True
        with self.assertRaises(ConnectionResetError):
            await self._send(socket, PLAYING)

        self.assertEqual(self.recorder.group_updates(_connection(socket)), [STOPPED])

    async def test_recording_group_updates_leaves_the_activation_as_sent(self) -> None:
        socket = object()
        await self._send(socket, ACTIVATE, STOPPED)

        self.assertEqual(self.recorder.initial_activation(_connection(socket)), ACTIVATE)


def _section(*group_updates: Any, label: str = "aiosendspin") -> str:
    return _render_group_update_section({"group_updates": list(group_updates)}, label=label)


class GroupUpdateSectionTests(unittest.TestCase):
    def test_shows_every_field_each_message_carried(self) -> None:
        section = _section(STOPPED, PLAYING)

        self.assertIn("1 of 2", section)
        self.assertIn("2 of 2", section)
        self.assertIn("&quot;stopped&quot;", section)
        self.assertIn("&quot;playing&quot;", section)
        self.assertEqual(section.count("&quot;group-1&quot;"), 2)
        self.assertEqual(section.count("&quot;Kitchen&quot;"), 2)
        self.assertNotIn("observed", section)

    def test_marks_a_required_field_a_message_left_out(self) -> None:
        section = _section({"type": "group/update", "payload": {"playback_state": "playing"}})

        self.assertEqual(section.count("not in the message"), 2)

    def test_says_none_followed_an_activate_rather_than_that_none_was_sent(self) -> None:
        section = _section(label="sendspin-go")

        self.assertIn("observed after a", section)
        self.assertIn("sendspin-go server sent no", section)
        self.assertIn("never carried a", section)

    def test_summary_without_the_list_renders_no_section(self) -> None:
        for summary in ({"status": "error"}, {"group_updates": None}, None):
            with self.subTest(summary=summary):
                self.assertEqual(_render_group_update_section(summary, label="aiosendspin"), "")

    def test_escapes_values_taken_from_the_wire(self) -> None:
        section = _section({"type": "group/update", "payload": {"group_name": "<script>"}})

        self.assertNotIn("<script>", section)
        self.assertIn("&lt;script&gt;", section)

    def test_shows_a_malformed_recording_whole(self) -> None:
        section = _section("garbled")

        self.assertIn("Recorded value", section)
        self.assertIn("&quot;garbled&quot;", section)

    def test_shows_a_recording_of_another_message_whole(self) -> None:
        section = _section({"type": "server/activate", "payload": STOPPED["payload"]})

        self.assertIn("Recorded value", section)
        self.assertIn("server/activate", section.split("Recorded value")[1])
        self.assertNotIn("not in the message", section)
        self.assertNotIn(">playback_state<", section)


if __name__ == "__main__":
    unittest.main()
