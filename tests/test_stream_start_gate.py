"""
Coverage for the available gate on ``stream/start`` and the trace it is judged from.

The aiosendspin server holds ``stream/start`` until the client reports available,
and every sendspin-go server case fails earlier on its missing ``server/activate``,
so the published matrix never reaches a violation. The rule is exercised here
against synthetic traces, and the recorder against stand-ins for the SDK transport.
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
from conformance.protocol import stream_start_gate_violation
from conformance.runner import _compare_summaries
from conformance.scenarios import SCENARIO_LIST, require_scenario

PCM_HASH = "a" * 64
PLAYBACK_ACTIVATE = {
    "type": "server/activate",
    "payload": {"activities": ["playback"], "active_roles": ["player@v1"]},
}

GROUP_UPDATE = {
    "type": "group/update",
    "payload": {"playback_state": "stopped", "group_id": "group-1", "group_name": "Kitchen"},
}

NO_AVAILABLE = object()


def _state(available: Any = NO_AVAILABLE) -> dict[str, Any]:
    return {"type": "client/state", "available": None if available is NO_AVAILABLE else available}


def _start(phase: str, *roles: str) -> dict[str, Any]:
    return {"type": "stream/start", "phase": phase, "roles": list(roles or ("player",))}


SENDING = _start("sending")
SENT = _start("sent")


def _violation(*trace: dict[str, Any], **summary: Any) -> str | None:
    return stream_start_gate_violation({"availability_trace": list(trace), **summary})


class StreamStartGateViolationTest(unittest.TestCase):
    """The rule itself, independent of any scenario's own verification."""

    def test_a_stream_started_after_the_client_reported_available_passes(self) -> None:
        traces = {
            "available from the start": (_state(True), SENDING, SENT),
            "available after syncing": (_state(False), _state(True), SENDING, SENT),
            "restarted while still available": (_state(True), SENDING, SENT, SENDING, SENT),
        }
        for label, trace in traces.items():
            with self.subTest(label):
                self.assertIsNone(_violation(*trace))

    def test_a_stream_started_before_any_client_state_names_the_server(self) -> None:
        violation = _violation(SENDING, SENT, _state(True))

        assert violation is not None
        self.assertTrue(violation.startswith("Server sent stream/start (player) before"))
        self.assertIn("any client/state", violation)

    def test_a_stream_started_while_unavailable_names_the_server(self) -> None:
        traces = {
            "never available": (_state(False), SENDING, SENT),
            "became unavailable": (_state(True), _state(False), SENDING, SENT),
            "second stream after becoming unavailable": (
                _state(True),
                SENDING,
                SENT,
                _state(False),
                SENDING,
                SENT,
            ),
        }
        for label, trace in traces.items():
            with self.subTest(label):
                violation = _violation(*trace)

                assert violation is not None
                self.assertTrue(violation.startswith("Server sent stream/start (player) while"))
                self.assertIn("available: false", violation)

    def test_a_state_arriving_during_the_write_is_read_in_the_servers_favour(self) -> None:
        traces = {
            "became available during the write": (_state(False), SENDING, _state(True), SENT),
            "became unavailable during the write": (_state(True), SENDING, _state(False), SENT),
            "first state during the write": (SENDING, _state(True), SENT),
        }
        for label, trace in traces.items():
            with self.subTest(label):
                self.assertIsNone(_violation(*trace))

    def test_a_failure_is_described_by_the_state_held_when_the_frame_was_let_go(self) -> None:
        violation = _violation(SENDING, _state(), _state(False), SENT)

        assert violation is not None
        self.assertIn("before it had received any client/state", violation)

    def test_a_state_without_available_names_the_client_first(self) -> None:
        for label, state in {"omitted": _state(), "not a boolean": _state("yes")}.items():
            with self.subTest(label):
                violation = _violation(_state(True), state, SENDING, SENT)

                assert violation is not None
                self.assertTrue(violation.startswith("Client sent client/state without"))
                self.assertIn("the server then sent stream/start (player)", violation)

    def test_the_reason_names_the_roles_the_stream_start_carried(self) -> None:
        violation = _violation(_start("sending", "artwork"), _start("sent", "artwork"))

        assert violation is not None
        self.assertIn("stream/start (artwork)", violation)

    def test_a_stream_start_that_never_reached_the_wire_is_not_judged(self) -> None:
        self.assertIsNone(_violation(_state(False), SENDING, _state(True), SENDING, SENT))

    def test_an_unrecorded_trace_is_a_harness_gap(self) -> None:
        summaries = {
            "absent": {},
            "null": {"availability_trace": None},
            "not a list": {"availability_trace": "garbled"},
            "unknown entry": {"availability_trace": [{"type": "server/time"}]},
            "unknown phase": {"availability_trace": [_start("queued")]},
            "sent without sending": {"availability_trace": [_state(True), SENT]},
            "no stream/start": {"availability_trace": [_state(True)]},
            "no stream/start reached the wire": {"availability_trace": [_state(False), SENDING]},
            "empty": {"availability_trace": []},
        }
        for label, summary in summaries.items():
            with self.subTest(label):
                violation = stream_start_gate_violation(summary)

                assert violation is not None
                self.assertTrue(violation.startswith(HARNESS_GAP))
                self.assertNotIn("Server sent", violation)


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

    def _verdict(self, scenario_id: str = "server-initiated-pcm", **server: Any) -> tuple[bool, str]:
        return _compare_summaries(
            require_scenario(scenario_id),
            _audio_summary(
                "server",
                **{
                    "activation": PLAYBACK_ACTIVATE,
                    "group_updates": [GROUP_UPDATE],
                    "time_exchange": [],
                    **server,
                },
            ),
            _audio_summary("client"),
        )

    def test_a_case_that_otherwise_passes_fails_on_an_early_stream_start(self) -> None:
        matches, reason = self._verdict(availability_trace=[_state(False), SENDING, SENT])

        self.assertFalse(matches)
        self.assertIn("reported available: false", reason)

    def test_a_case_whose_server_recorded_no_trace_fails_as_a_harness_gap(self) -> None:
        for label, server in {"absent": {}, "null": {"availability_trace": None}}.items():
            with self.subTest(label):
                matches, reason = self._verdict(**server)

                self.assertFalse(matches)
                self.assertTrue(reason.startswith(HARNESS_GAP))

    def test_a_case_whose_stream_waited_for_the_client_passes(self) -> None:
        matches, _ = self._verdict(availability_trace=[_state(True), SENDING, SENT])

        self.assertTrue(matches)

    def test_a_missing_activation_is_reported_first(self) -> None:
        matches, reason = self._verdict(activation=None, availability_trace=[SENDING, SENT])

        self.assertFalse(matches)
        self.assertIn("server/activate", reason)

    def test_only_scenarios_that_open_a_stream_are_judged(self) -> None:
        with mock.patch("conformance.runner._dispatch_comparison", return_value=(True, "ok")):
            verdicts = {
                scenario.id: _compare_summaries(
                    scenario,
                    {"status": "ok", "group_updates": [GROUP_UPDATE], "time_exchange": []},
                    {"status": "ok"},
                )
                for scenario in SCENARIO_LIST
            }

        unjudged = {scenario_id for scenario_id, (matches, _) in verdicts.items() if matches}
        self.assertEqual(
            unjudged,
            {
                "server-initiated-metadata",
                "server-initiated-controller",
                "client-initiated-metadata",
                "client-initiated-controller",
            },
        )
        for scenario_id in verdicts.keys() - unjudged:
            with self.subTest(scenario_id):
                self.assertTrue(verdicts[scenario_id][1].startswith(HARNESS_GAP))


def _transport(socket: object) -> EncryptedWebSocket:
    """Build a transport bound to ``socket`` without running a Noise handshake."""
    transport = object.__new__(EncryptedWebSocket)
    transport._ws = socket
    return transport


def _connection(socket: object, *, encrypted: bool = True) -> Any:
    return SimpleNamespace(_wsock_server=socket, _wsock_client=None, is_encrypted=encrypted)


def _message(message_type: str, **payload: Any) -> str:
    return json.dumps({"type": message_type, "payload": payload})


class ControlMessageRecorderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.incoming: list[Any] = []
        self.recorder: ControlMessageRecorder

        async def send_str(transport: EncryptedWebSocket, _data: str) -> None:
            # What the recorder holds while the frame is still being written.
            self.during_send = self.recorder.availability_trace(_connection(transport._ws))

        async def receive(_transport: EncryptedWebSocket) -> Any:
            return self.incoming.pop(0)

        for name, stand_in in (("send_str", send_str), ("receive", receive)):
            patcher = mock.patch.object(EncryptedWebSocket, name, stand_in)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.recorder = ControlMessageRecorder()
        self.addCleanup(self.recorder.uninstall)

    async def _receive(self, transport: EncryptedWebSocket, data: Any, kind: Any = WSMsgType.TEXT) -> Any:
        self.incoming.append(SimpleNamespace(type=kind, data=data))
        return await transport.receive()

    async def test_records_states_and_stream_starts_in_order(self) -> None:
        socket = object()
        transport = _transport(socket)
        await self._receive(transport, _message("client/state", available=False))
        await self._receive(transport, _message("client/state", available=True, player={}))
        await transport.send_str(_message("stream/start", server_transmitted=1, player={}))

        self.assertEqual(
            self.recorder.availability_trace(_connection(socket)),
            [_state(False), _state(True), SENDING, SENT],
        )

    async def test_one_recorder_keeps_every_view_of_a_connection(self) -> None:
        socket = object()
        transport = _transport(socket)
        await transport.send_str(json.dumps(PLAYBACK_ACTIVATE))
        await transport.send_str(json.dumps(GROUP_UPDATE))
        await self._receive(transport, _message("client/state", available=True))
        await transport.send_str(_message("stream/start", player={}))

        connection = _connection(socket)
        self.assertEqual(self.recorder.initial_activation(connection), PLAYBACK_ACTIVATE)
        self.assertEqual(self.recorder.group_updates(connection), [GROUP_UPDATE])
        self.assertEqual(
            self.recorder.availability_trace(connection), [_state(True), SENDING, SENT]
        )

    async def test_a_stream_start_is_marked_sending_until_it_is_written(self) -> None:
        socket = object()
        await _transport(socket).send_str(_message("stream/start", artwork={}, player={}))

        self.assertEqual(self.during_send, [_start("sending", "artwork", "player")])

    async def test_a_stream_start_that_failed_to_send_is_never_marked_sent(self) -> None:
        async def failing_send_str(_transport: EncryptedWebSocket, _data: str) -> None:
            raise ConnectionResetError

        socket = object()
        with mock.patch.object(self.recorder, "_original_send_str", failing_send_str):
            with self.assertRaises(ConnectionResetError):
                await _transport(socket).send_str(_message("stream/start", player={}))

        self.assertEqual(self.recorder.availability_trace(_connection(socket)), [SENDING])

    async def test_records_available_as_it_arrived(self) -> None:
        socket = object()
        transport = _transport(socket)
        await self._receive(transport, _message("client/state", state="synchronized"))
        await self._receive(transport, _message("client/state", available="yes"))

        self.assertEqual(self.recorder.availability_trace(_connection(socket)), [_state(), _state("yes")])

    async def test_records_a_state_whose_type_was_written_with_an_escaped_slash(self) -> None:
        socket = object()
        await self._receive(
            _transport(socket), '{"type":"client\\/state","payload":{"available":true}}'
        )

        self.assertEqual(self.recorder.availability_trace(_connection(socket)), [_state(True)])

    async def test_other_messages_are_not_recorded(self) -> None:
        socket = object()
        transport = _transport(socket)
        await self._receive(transport, _message("client/command", note="client/state"))
        await self._receive(transport, b'{"type": "client/state"}', WSMsgType.BINARY)
        await self._receive(transport, "client/state, but not JSON")
        await transport.send_str(_message("server/state", note="stream/start"))

        self.assertIsNone(self.recorder.availability_trace(_connection(socket)))

    async def test_returns_what_the_transport_received(self) -> None:
        received = await self._receive(_transport(object()), _message("client/state"))

        self.assertEqual(received.data, _message("client/state"))

    async def test_keeps_connections_apart(self) -> None:
        first, second = object(), object()
        await self._receive(_transport(first), _message("client/state", available=True))
        await _transport(second).send_str(_message("stream/start", player={}))

        self.assertEqual(self.recorder.availability_trace(_connection(first)), [_state(True)])
        self.assertEqual(self.recorder.availability_trace(_connection(second)), [SENDING, SENT])

    def test_a_connection_nothing_was_observed_on_reports_no_trace(self) -> None:
        self.assertIsNone(self.recorder.availability_trace(_connection(object())))

    async def test_uninstall_stops_recording(self) -> None:
        socket = object()
        self.recorder.uninstall()
        await _transport(socket).send_str(_message("stream/start", player={}))

        self.assertIsNone(self.recorder.availability_trace(_connection(socket)))


if __name__ == "__main__":
    unittest.main()
