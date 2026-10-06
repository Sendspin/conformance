"""Coverage for the controller state the matrix compares across a case.

RC1 makes `volume`, `muted` and `supported_commands` required members of the
`server/state` controller object, so a case must fail when the client observes
something other than what the server advertised. Reproducing that over the wire
needs two adapters and a live connection, so the comparison rules are exercised
here against synthetic summaries shaped like the ones the adapters really write.
The aiosendspin server's half — reading the advertised set off the wire rather
than reconstructing it — is exercised against real SDK message objects.
"""

from __future__ import annotations

import unittest
from typing import Any

from conformance.adapters.aiosendspin_server import _controller_state_from_message
from conformance.runner import _compare_summaries
from conformance.scenarios import require_scenario

_ABSENT = object()

COMMAND = "next"
ADVERTISED = ["mute", "next", "switch", "volume"]


def _apply(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Apply overrides, with the `_ABSENT` sentinel dropping a field outright."""
    for field, value in overrides.items():
        if value is _ABSENT:
            base.pop(field, None)
        else:
            base[field] = value
    return base


def _server_summary(**overrides: Any) -> dict[str, Any]:
    controller = {
        "expected_command": {"command": COMMAND},
        "received_command": {"command": COMMAND},
        "supported_commands": list(ADVERTISED),
        "volume": 100,
        "muted": False,
        "repeat": "all",
        "shuffle": False,
    }
    return {
        "status": "ok",
        "implementation": "synthetic-server",
        "role": "server",
        "controller": _apply(controller, overrides),
    }


def _client_summary(**overrides: Any) -> dict[str, Any]:
    received_state = {
        "supported_commands": list(ADVERTISED),
        "volume": 100,
        "muted": False,
        "repeat": "all",
        "shuffle": False,
    }
    return {
        "status": "ok",
        "implementation": "synthetic-client",
        "role": "client",
        "controller": {
            "sent_command": {"command": COMMAND},
            "received_state": _apply(received_state, overrides),
        },
    }


class ControllerStateComparisonTests(unittest.TestCase):
    """The controller case must agree on every field of the controller state."""

    def test_matching_state_passes(self) -> None:
        matches, reason = self._compare(_server_summary(), _client_summary())
        self.assertTrue(matches, reason)

    def test_volume_mismatch_fails(self) -> None:
        matches, reason = self._compare(_server_summary(), _client_summary(volume=42))
        self.assertFalse(matches)
        self.assertIn("Controller volume mismatch", reason)

    def test_missing_volume_fails(self) -> None:
        matches, reason = self._compare(_server_summary(), _client_summary(volume=_ABSENT))
        self.assertFalse(matches)
        self.assertIn("missing volume", reason)

    def test_muted_mismatch_fails(self) -> None:
        matches, reason = self._compare(_server_summary(), _client_summary(muted=True))
        self.assertFalse(matches)
        self.assertIn("Controller muted mismatch", reason)

    def test_missing_muted_fails(self) -> None:
        matches, reason = self._compare(_server_summary(), _client_summary(muted=_ABSENT))
        self.assertFalse(matches)
        self.assertIn("missing muted", reason)

    def test_command_the_client_never_saw_advertised_fails(self) -> None:
        """A client dropping a command it could not parse is a conformance signal."""
        matches, reason = self._compare(
            _server_summary(),
            _client_summary(supported_commands=["mute", "next", "volume"]),
        )
        self.assertFalse(matches)
        self.assertIn("advertised but not observed: ['switch']", reason)

    def test_command_the_server_never_advertised_fails(self) -> None:
        matches, reason = self._compare(
            _server_summary(),
            _client_summary(supported_commands=[*ADVERTISED, "seek"]),
        )
        self.assertFalse(matches)
        self.assertIn("observed but not advertised: ['seek']", reason)

    def test_both_directions_are_reported_together(self) -> None:
        matches, reason = self._compare(
            _server_summary(),
            _client_summary(supported_commands=["mute", "next", "volume", "seek"]),
        )
        self.assertFalse(matches)
        self.assertIn("advertised but not observed: ['switch']", reason)
        self.assertIn("observed but not advertised: ['seek']", reason)

    def test_supported_commands_order_is_not_significant(self) -> None:
        """A real aiosendspin pairing reports the same set in a different order."""
        matches, reason = self._compare(
            _server_summary(),
            _client_summary(supported_commands=list(reversed(ADVERTISED))),
        )
        self.assertTrue(matches, reason)

    def test_missing_supported_commands_fails(self) -> None:
        matches, reason = self._compare(
            _server_summary(),
            _client_summary(supported_commands=_ABSENT),
        )
        self.assertFalse(matches)
        self.assertIn("missing supported_commands", reason)

    def test_server_without_advertised_commands_fails(self) -> None:
        matches, reason = self._compare(
            _server_summary(supported_commands=_ABSENT),
            _client_summary(),
        )
        self.assertFalse(matches)
        self.assertIn("no advertised supported_commands", reason)

    def test_unadvertised_command_under_test_fails(self) -> None:
        """RC1: a command MUST be one the latest controller state listed."""
        matches, reason = self._compare(
            _server_summary(supported_commands=["volume"]),
            _client_summary(supported_commands=["volume"]),
        )
        self.assertFalse(matches)
        self.assertIn("is absent from the supported_commands", reason)

    def test_repeat_and_shuffle_still_compared(self) -> None:
        matches, reason = self._compare(_server_summary(), _client_summary(shuffle=True))
        self.assertFalse(matches)
        self.assertIn("Controller shuffle mismatch", reason)

    def _compare(
        self,
        server: dict[str, Any],
        client: dict[str, Any],
    ) -> tuple[bool, str]:
        return _compare_summaries(
            require_scenario("server-initiated-controller"),
            server,
            client,
        )


class AdvertisedControllerStateTests(unittest.TestCase):
    """The server must report the state it put on the wire, not a reconstruction."""

    def test_controller_state_is_read_off_the_message(self) -> None:
        from aiosendspin.models.types import MediaCommand, RepeatMode

        state = _controller_state_from_message(
            self._server_state(
                supported_commands=[
                    MediaCommand.SWITCH,
                    MediaCommand.VOLUME,
                    MediaCommand.MUTE,
                    MediaCommand.NEXT,
                ],
                volume=42,
                muted=True,
                repeat=RepeatMode.ALL,
                shuffle=True,
            )
        )
        self.assertEqual(
            state,
            {
                "supported_commands": ["mute", "next", "switch", "volume"],
                "volume": 42,
                "muted": True,
                "repeat": "all",
                "shuffle": True,
            },
        )

    def test_commands_are_sorted_so_the_set_reads_stably(self) -> None:
        """The SDK builds the list from a set, so its order is not reproducible."""
        from aiosendspin.models.types import MediaCommand

        state = _controller_state_from_message(
            self._server_state(supported_commands=[MediaCommand.VOLUME, MediaCommand.MUTE])
        )
        assert state is not None
        self.assertEqual(state["supported_commands"], ["mute", "volume"])

    def test_state_without_a_controller_object_is_ignored(self) -> None:
        """An absent field is an UndefinedField sentinel, not None."""
        from aiosendspin.models.core import ServerStateMessage, ServerStatePayload

        self.assertIsNone(_controller_state_from_message(ServerStateMessage(ServerStatePayload())))

    def test_a_metadata_only_state_is_ignored(self) -> None:
        """The repeat/shuffle back-compat mirror emits one of these alongside ours."""
        from aiosendspin.models.core import ServerStateMessage, ServerStatePayload
        from aiosendspin.models.metadata import SessionUpdateMetadata

        message = ServerStateMessage(
            ServerStatePayload(metadata=SessionUpdateMetadata(timestamp=1, title="Something"))
        )
        self.assertIsNone(_controller_state_from_message(message))

    def test_another_message_type_is_ignored(self) -> None:
        from aiosendspin.models.core import ServerTimeMessage, ServerTimePayload

        message = ServerTimeMessage(
            ServerTimePayload(client_transmitted=1, server_received=2, server_transmitted=3)
        )
        self.assertIsNone(_controller_state_from_message(message))

    @staticmethod
    def _server_state(**fields: Any) -> Any:
        from aiosendspin.models.controller import ControllerStatePayload
        from aiosendspin.models.core import ServerStateMessage, ServerStatePayload
        from aiosendspin.models.types import MediaCommand, RepeatMode

        defaults: dict[str, Any] = {
            "supported_commands": [MediaCommand.NEXT],
            "volume": 100,
            "muted": False,
            "repeat": RepeatMode.OFF,
            "shuffle": False,
        }
        defaults.update(fields)
        return ServerStateMessage(
            ServerStatePayload(controller=ControllerStatePayload(**defaults))
        )


if __name__ == "__main__":
    unittest.main()
