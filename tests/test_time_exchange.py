"""
Coverage for the verdict on the ``client/time`` to ``server/time`` exchange.

The aiosendspin server answers every ``client/time`` it reads, and every
sendspin-go server case fails earlier on its missing ``server/activate``, so the
published matrix never reaches a violation. The rule is exercised here against
synthetic exchanges, and the recorder against stand-ins for the SDK transport.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from aiohttp import WSMsgType
from aiosendspin.noise.wire import EncryptedWebSocket

from conformance.adapters._aiosendspin_protocol_evidence import ControlMessageRecorder
from conformance.chunk_framing import HARNESS_GAP
from conformance.protocol import time_exchange_violation
from conformance.runner import _compare_summaries
from conformance.scenarios import SCENARIO_LIST, require_scenario

PCM_HASH = "a" * 64
ACTIVATE = {
    "type": "server/activate",
    "payload": {"activities": ["playback"], "active_roles": ["player@v1"]},
}
GROUP_UPDATE = {
    "type": "group/update",
    "payload": {"playback_state": "stopped", "group_id": "group-1", "group_name": "Kitchen"},
}
AVAILABILITY_TRACE = [
    {"type": "client/state", "available": True},
    {"type": "stream/start", "phase": "sending", "roles": ["player"]},
    {"type": "stream/start", "phase": "sent", "roles": ["player"]},
]


def _client_time(client_transmitted: Any = 100) -> dict[str, Any]:
    return {"type": "client/time", "payload": {"client_transmitted": client_transmitted}}


def _server_time(client_transmitted: Any = 100, **overrides: Any) -> dict[str, Any]:
    payload = {
        "client_transmitted": client_transmitted,
        "server_received": 7,
        "server_transmitted": 9,
        **overrides,
    }
    return {"type": "server/time", "payload": payload}


OTHER_SENT = {"type": "other-sent"}


def _without(name: str) -> dict[str, Any]:
    message = _server_time()
    del message["payload"][name]
    return message


def _violation(*exchange: Any) -> str | None:
    return time_exchange_violation({"time_exchange": list(exchange)})


class TimeExchangeViolationTest(unittest.TestCase):
    """The rule itself, against the `time_exchange` field of a server summary."""

    def test_answered_client_times_are_not_reported(self) -> None:
        for label, exchange in {
            "one": [_client_time(), _server_time()],
            "several": [_client_time(1), _server_time(1), _client_time(2), _server_time(2)],
            "answered after the next arrived": [
                _client_time(1),
                _client_time(2),
                _server_time(1),
                _server_time(2),
            ],
            "answered out of order": [
                _client_time(1),
                _client_time(2),
                _server_time(2),
                _server_time(1),
            ],
            "the same value sent twice": [
                _client_time(1),
                _client_time(1),
                _server_time(1),
                _server_time(1),
            ],
            "another message sent before the reply": [_client_time(), OTHER_SENT, _server_time()],
            "equal server timestamps": [
                _client_time(),
                _server_time(server_received=9, server_transmitted=9),
            ],
        }.items():
            with self.subTest(label):
                self.assertIsNone(_violation(*exchange))

    def test_a_client_that_sent_no_client_time_gives_nothing_to_judge(self) -> None:
        self.assertIsNone(_violation())

    def test_timestamps_far_from_the_epoch_or_below_zero_are_not_judged(self) -> None:
        for received, transmitted in ((-50, -40), (0, 0), (3, 2**62)):
            with self.subTest(received=received):
                self.assertIsNone(
                    _violation(
                        _client_time(-5),
                        _server_time(-5, server_received=received, server_transmitted=transmitted),
                    )
                )

    def test_a_missing_field_is_reported(self) -> None:
        for name in ("client_transmitted", "server_received", "server_transmitted"):
            with self.subTest(name):
                violation = _violation(_client_time(), _without(name))

                self.assertIsNotNone(violation)
                self.assertTrue(violation.startswith("Server's server/time 1 "), violation)
                self.assertIn(f"omitted {name}", violation)

    def test_a_field_that_is_not_an_integer_is_reported(self) -> None:
        for name in ("client_transmitted", "server_received", "server_transmitted"):
            for value in ("100", 100.0, None, True, [100]):
                with self.subTest(name=name, value=value):
                    violation = _violation(_client_time(), _server_time(**{name: value}))

                    self.assertIsNotNone(violation)
                    self.assertIn(f"declared {name} {json.dumps(value)}", violation)
                    self.assertIn("requires an integer", violation)

    def test_a_server_time_without_a_payload_object_is_reported(self) -> None:
        violation = _violation(_client_time(), {"type": "server/time"})

        self.assertIsNotNone(violation)
        self.assertIn("no payload object", violation)

    def test_a_client_transmitted_the_client_never_sent_is_reported(self) -> None:
        violation = _violation(_client_time(100), _server_time(101))

        self.assertIsNotNone(violation)
        self.assertTrue(violation.startswith("Server's server/time 1 carried"), violation)
        self.assertIn("client_transmitted 101", violation)

    def test_an_echo_is_spent_by_the_server_time_that_carried_it(self) -> None:
        violation = _violation(_client_time(1), _server_time(1), _server_time(1))

        self.assertIsNotNone(violation)
        self.assertTrue(violation.startswith("Server's server/time 2 carried"), violation)

    def test_a_server_time_no_client_time_preceded_is_reported(self) -> None:
        violation = _violation(_server_time(1), _client_time(1))

        self.assertIsNotNone(violation)
        self.assertIn("not the value of any client/time", violation)

    def test_server_received_after_server_transmitted_is_reported(self) -> None:
        violation = _violation(
            _client_time(), _server_time(server_received=10, server_transmitted=9)
        )

        self.assertIsNotNone(violation)
        self.assertIn("server_received 10 later than its server_transmitted 9", violation)

    def test_a_client_time_the_server_sent_past_is_reported(self) -> None:
        for label, exchange in {
            "never answered": [_client_time(1), OTHER_SENT],
            "second of two": [_client_time(1), _server_time(1), _client_time(2), OTHER_SENT],
            "first of two": [_client_time(1), OTHER_SENT, _client_time(2)],
        }.items():
            with self.subTest(label):
                violation = _violation(*exchange)

                self.assertIsNotNone(violation)
                self.assertIn("yet sent another message after receiving it", violation)

    def test_the_client_time_named_is_the_one_sent_past(self) -> None:
        violation = _violation(
            _client_time(1), OTHER_SENT, _server_time(1), _client_time(2), OTHER_SENT
        )

        self.assertIsNotNone(violation)
        self.assertTrue(
            violation.startswith("Server sent no server/time for client/time 2 of 2"), violation
        )

    def test_a_client_time_skipped_for_a_later_one_is_reported(self) -> None:
        violation = _violation(_client_time(1), _client_time(2), _server_time(2))

        self.assertIsNotNone(violation)
        self.assertTrue(
            violation.startswith("Server sent no server/time for client/time 1 of 2"), violation
        )

    def test_client_times_still_unanswered_when_the_case_ended_are_not_judged(self) -> None:
        for label, exchange in {
            "the only one": [_client_time(1)],
            "every one": [_client_time(1), _client_time(2), _client_time(3)],
            # Sent before the client/time was received, so no evidence about it.
            "after another message": [OTHER_SENT, _client_time(1)],
            "last one": [_client_time(1), _server_time(1), _client_time(2)],
            "last two": [_client_time(1), _server_time(1), _client_time(2), _client_time(3)],
            # The reply to 1 was still being written when 2 arrived.
            "arrived during the last reply": [_client_time(1), _client_time(2), _server_time(1)],
        }.items():
            with self.subTest(label):
                self.assertIsNone(_violation(*exchange))

    def test_a_client_time_without_an_integer_names_the_client(self) -> None:
        for exchange in (
            [_client_time("100"), _server_time("100")],
            [{"type": "client/time"}],
            [_client_time(1), _server_time(1), _client_time(True)],
        ):
            with self.subTest(exchange=exchange):
                violation = _violation(*exchange)

                self.assertIsNotNone(violation)
                self.assertTrue(violation.startswith("Client's client/time "), violation)

    def test_an_exchange_that_was_not_recorded_is_a_harness_gap(self) -> None:
        for summary in (
            {"status": "ok"},
            {"time_exchange": None},
            {"time_exchange": _client_time()},
            {"time_exchange": [_client_time(), GROUP_UPDATE]},
            {"time_exchange": ["client/time"]},
        ):
            with self.subTest(summary=summary):
                violation = time_exchange_violation(summary)

                self.assertIsNotNone(violation)
                self.assertTrue(violation.startswith(HARNESS_GAP), violation)


def _audio_summary(role: str, **extra: Any) -> dict[str, Any]:
    """Build a summary that passes PCM verification on its own."""
    return {
        "status": "ok",
        "implementation": f"synthetic-{role}",
        "role": role,
        "audio": {
            "received_pcm_sha256" if role == "client" else "source_pcm_sha256": PCM_HASH,
            "audio_chunk_count": 4,
        },
        **extra,
    }


class CaseVerdictTest(unittest.TestCase):
    """The rule as it reaches a case result."""

    def _verdict(self, **server: Any) -> tuple[bool, str]:
        return _compare_summaries(
            require_scenario("server-initiated-pcm"),
            _audio_summary(
                "server",
                **{
                    "activation": ACTIVATE,
                    "group_updates": [GROUP_UPDATE],
                    "availability_trace": AVAILABILITY_TRACE,
                    **server,
                },
            ),
            _audio_summary("client"),
        )

    def test_a_case_with_a_conformant_exchange_passes(self) -> None:
        matches, _ = self._verdict(time_exchange=[_client_time(), _server_time()])

        self.assertTrue(matches)

    def test_a_case_that_otherwise_passes_fails_on_a_wrong_echo(self) -> None:
        matches, reason = self._verdict(time_exchange=[_client_time(1), _server_time(2)])

        self.assertFalse(matches)
        self.assertIn("client_transmitted 2", reason)

    def test_a_case_whose_server_recorded_no_exchange_never_passes(self) -> None:
        for label, server in {"absent": {}, "null": {"time_exchange": None}}.items():
            with self.subTest(label):
                matches, reason = self._verdict(**server)

                self.assertFalse(matches)
                self.assertTrue(reason.startswith(HARNESS_GAP), reason)

    def test_a_missing_group_update_is_reported_first(self) -> None:
        matches, reason = self._verdict(
            group_updates=[], time_exchange=[_client_time(), OTHER_SENT]
        )

        self.assertFalse(matches)
        self.assertIn("Server sent no group/update", reason)

    def test_every_scenario_is_judged(self) -> None:
        server = {
            "status": "ok",
            "group_updates": [GROUP_UPDATE],
            "availability_trace": AVAILABILITY_TRACE,
            "time_exchange": [_client_time(), OTHER_SENT],
        }
        with mock.patch("conformance.runner._dispatch_comparison", return_value=(True, "ok")):
            for scenario in SCENARIO_LIST:
                with self.subTest(scenario.id):
                    matches, reason = _compare_summaries(scenario, server, {"status": "ok"})

                    self.assertFalse(matches)
                    self.assertIn("sent no server/time", reason)


def _transport(socket: object) -> EncryptedWebSocket:
    """Build a transport bound to ``socket`` without running a Noise handshake."""
    transport = EncryptedWebSocket.__new__(EncryptedWebSocket)
    transport._ws = socket
    return transport


def _connection(socket: object) -> Any:
    return SimpleNamespace(_wsock_server=socket, _wsock_client=None, is_encrypted=True)


class ControlMessageRecorderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.incoming: list[Any] = []

        async def send_str(_transport: EncryptedWebSocket, _data: str) -> None:
            return None

        async def receive(_transport: EncryptedWebSocket) -> Any:
            return self.incoming.pop(0)

        for name, stand_in in (("send_str", send_str), ("receive", receive)):
            patcher = mock.patch.object(EncryptedWebSocket, name, stand_in)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.recorder = ControlMessageRecorder()
        self.addCleanup(self.recorder.uninstall)

    async def _receive(
        self, transport: EncryptedWebSocket, data: Any, kind: Any = WSMsgType.TEXT
    ) -> Any:
        self.incoming.append(SimpleNamespace(type=kind, data=data))
        return await transport.receive()

    async def test_records_the_exchange_in_order_as_transported(self) -> None:
        socket = object()
        transport = _transport(socket)
        await self._receive(transport, json.dumps(_client_time(1)))
        await self._receive(transport, json.dumps(_client_time(2)))
        await transport.send_str(json.dumps(_server_time(1)))
        await transport.send_str(json.dumps(_without("server_received")))

        self.assertEqual(
            self.recorder.time_exchange(_connection(socket)),
            [_client_time(1), _client_time(2), _server_time(1), _without("server_received")],
        )

    async def test_records_a_client_time_whose_type_was_written_with_an_escaped_slash(
        self,
    ) -> None:
        socket = object()
        await self._receive(
            _transport(socket), '{"type":"client\\/time","payload":{"client_transmitted":5}}'
        )

        self.assertEqual(self.recorder.time_exchange(_connection(socket)), [_client_time(5)])

    async def test_a_server_time_that_failed_to_send_is_not_recorded(self) -> None:
        async def failing_send_str(_transport: EncryptedWebSocket, _data: str) -> None:
            raise ConnectionResetError

        socket = object()
        transport = _transport(socket)
        await self._receive(transport, json.dumps(_client_time()))
        with mock.patch.object(self.recorder, "_original_send_str", failing_send_str):
            with self.assertRaises(ConnectionResetError):
                await transport.send_str(json.dumps(_server_time()))

        self.assertEqual(self.recorder.time_exchange(_connection(socket)), [_client_time()])

    async def test_a_client_that_sent_no_client_time_reads_as_an_empty_exchange(self) -> None:
        socket = object()
        transport = _transport(socket)
        await self._receive(transport, json.dumps({"type": "client/state", "payload": {}}))
        await self._receive(transport, json.dumps(_client_time()), WSMsgType.BINARY)
        await transport.send_str(json.dumps(GROUP_UPDATE))

        self.assertEqual(self.recorder.time_exchange(_connection(socket)), [])

    async def test_marks_where_another_message_was_sent(self) -> None:
        socket = object()
        transport = _transport(socket)
        await transport.send_str(json.dumps(ACTIVATE))
        await self._receive(transport, json.dumps(_client_time(1)))
        await transport.send_str(json.dumps(GROUP_UPDATE))
        await transport.send_str(json.dumps({"type": "server/state", "payload": {}}))
        await transport.send_str(json.dumps(_server_time(1)))
        await transport.send_str(json.dumps({"type": "stream/start", "payload": {"player": {}}}))

        self.assertEqual(
            self.recorder.time_exchange(_connection(socket)),
            [_client_time(1), OTHER_SENT, _server_time(1), OTHER_SENT],
        )

    async def test_a_client_time_read_during_a_send_is_placed_after_it(self) -> None:
        socket = object()
        transport = _transport(socket)
        await self._receive(transport, json.dumps(_client_time(1)))

        async def send_while_reading(_transport: EncryptedWebSocket, _data: str) -> None:
            await self._receive(transport, json.dumps(_client_time(2)))

        with mock.patch.object(self.recorder, "_original_send_str", send_while_reading):
            await transport.send_str(json.dumps(GROUP_UPDATE))

        self.assertEqual(
            self.recorder.time_exchange(_connection(socket)),
            [_client_time(1), OTHER_SENT, _client_time(2)],
        )

    async def test_another_message_that_failed_to_send_is_not_marked(self) -> None:
        async def failing_send_str(_transport: EncryptedWebSocket, _data: str) -> None:
            raise ConnectionResetError

        socket = object()
        transport = _transport(socket)
        await self._receive(transport, json.dumps(_client_time()))
        with mock.patch.object(self.recorder, "_original_send_str", failing_send_str):
            for message in (GROUP_UPDATE, {"type": "stream/start", "payload": {}}):
                with self.assertRaises(ConnectionResetError):
                    await transport.send_str(json.dumps(message))

        self.assertEqual(self.recorder.time_exchange(_connection(socket)), [_client_time()])

    async def test_a_connection_no_text_was_observed_on_reports_no_exchange(self) -> None:
        socket = object()
        await _transport(socket).send_str(json.dumps(ACTIVATE))

        self.assertIsNone(self.recorder.time_exchange(_connection(socket)))
        self.assertIsNone(self.recorder.time_exchange(_connection(object())))

    async def test_keeps_connections_apart(self) -> None:
        first, second = object(), object()
        await self._receive(_transport(first), json.dumps(_client_time(1)))
        await self._receive(_transport(second), json.dumps(_client_time(2)))

        self.assertEqual(self.recorder.time_exchange(_connection(first)), [_client_time(1)])
        self.assertEqual(self.recorder.time_exchange(_connection(second)), [_client_time(2)])

    async def test_recording_the_exchange_leaves_the_other_views_as_they_were(self) -> None:
        socket = object()
        transport = _transport(socket)
        await transport.send_str(json.dumps(ACTIVATE))
        await transport.send_str(json.dumps(GROUP_UPDATE))
        await self._receive(transport, json.dumps(_client_time()))
        await transport.send_str(json.dumps(_server_time()))

        connection = _connection(socket)
        self.assertEqual(self.recorder.initial_activation(connection), ACTIVATE)
        self.assertEqual(self.recorder.group_updates(connection), [GROUP_UPDATE])
        self.assertIsNone(self.recorder.availability_trace(connection))


if __name__ == "__main__":
    unittest.main()
