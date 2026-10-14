"""
Coverage for the verdict drawn from a server's recorded ``group/update`` messages.

No server in the published matrix omits a ``group/update`` field or sends a
playback state RC1 does not define, and an adapter that records nothing is a
state the matrix only reaches when an adapter is broken. The rule is therefore
exercised here against synthetic summaries shaped like the ones the adapters
really write.
"""

from __future__ import annotations

import unittest
from typing import Any

from conformance.chunk_framing import HARNESS_GAP
from conformance.protocol import group_update_violation
from conformance.runner import _compare_summaries
from conformance.scenarios import require_scenario

PCM_HASH = "a" * 64

ACTIVATE = {
    "type": "server/activate",
    "payload": {"activities": ["playback"], "active_roles": ["player@v1"]},
}
GROUP = {"playback_state": "stopped", "group_id": "group-1", "group_name": "Kitchen"}


# What a server that held stream/start until the client reported available records.
AVAILABILITY_TRACE = [
    {"type": "client/state", "available": True},
    {"type": "stream/start", "phase": "sending", "roles": ["player"]},
    {"type": "stream/start", "phase": "sent", "roles": ["player"]},
]


def _group_update(**payload: Any) -> dict[str, Any]:
    return {"type": "group/update", "payload": payload}


def _without(name: str) -> dict[str, Any]:
    return _group_update(**{key: value for key, value in GROUP.items() if key != name})


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


def _violation(*group_updates: Any) -> str | None:
    return group_update_violation({"group_updates": list(group_updates)})


class GroupUpdateViolationTest(unittest.TestCase):
    """The rule itself, against the `group_updates` field of a server summary."""

    def test_no_group_update_sent_is_reported_as_a_server_defect(self) -> None:
        violation = _violation()

        self.assertIsNotNone(violation)
        self.assertTrue(violation.startswith("Server sent no group/update"))
        self.assertIn("RC1", violation)

    def test_conformant_group_updates_are_not_reported(self) -> None:
        for group_update in (
            _group_update(**GROUP),
            _group_update(**{**GROUP, "playback_state": "playing"}),
            _group_update(**{**GROUP, "group_name": ""}),
            _group_update(**GROUP, extension="ignored"),
        ):
            with self.subTest(group_update=group_update):
                self.assertIsNone(_violation(group_update))

    def test_each_omitted_field_is_named(self) -> None:
        for name in GROUP:
            with self.subTest(omitted=name):
                violation = _violation(_without(name))

                self.assertIsNotNone(violation)
                self.assertTrue(violation.startswith("Server's group/update 1 of 1"))
                self.assertIn(f"omitted {name}", violation)

    def test_a_playback_state_rc1_does_not_define_is_reported(self) -> None:
        for playback_state in ("paused", "Playing", "", None, 1, ["playing"]):
            with self.subTest(playback_state=playback_state):
                violation = _violation(_group_update(**{**GROUP, "playback_state": playback_state}))

                self.assertIsNotNone(violation)
                self.assertIn("declared playback_state", violation)
                self.assertIn("'playing' or 'stopped'", violation)

    def test_a_group_field_that_is_not_a_string_is_reported(self) -> None:
        for name in ("group_id", "group_name"):
            for value in (None, 7, ["group-1"]):
                with self.subTest(name=name, value=value):
                    violation = _violation(_group_update(**{**GROUP, name: value}))

                    self.assertIsNotNone(violation)
                    self.assertIn(f"declared {name}", violation)
                    self.assertIn("requires a string", violation)

    def test_every_defect_in_one_group_update_is_named(self) -> None:
        violation = _violation(_group_update(playback_state="paused", group_id=7))

        self.assertIsNotNone(violation)
        self.assertIn("declared playback_state", violation)
        self.assertIn("declared group_id", violation)
        self.assertIn("omitted group_name", violation)

    def test_a_malformed_update_after_a_conformant_one_is_reported(self) -> None:
        conformant = _group_update(**GROUP)
        violation = _violation(conformant, conformant, _without("group_name"))

        self.assertIsNotNone(violation)
        self.assertTrue(violation.startswith("Server's group/update 3 of 3"), violation)
        self.assertIn("omitted group_name", violation)

    def test_every_update_conforming_is_not_reported(self) -> None:
        playing = _group_update(**{**GROUP, "playback_state": "playing"})

        self.assertIsNone(_violation(_group_update(**GROUP), playing, _group_update(**GROUP)))

    def test_a_summary_that_records_nothing_is_a_harness_gap(self) -> None:
        for summary in (
            {"status": "ok", "activation": ACTIVATE},
            {"group_updates": None},
            {"group_updates": _group_update(**GROUP)},
            {"group_update": _group_update(**GROUP)},
        ):
            with self.subTest(summary=summary):
                violation = group_update_violation(summary)

                self.assertIsNotNone(violation)
                self.assertTrue(violation.startswith(HARNESS_GAP), violation)
                self.assertIn("does not record the group/update messages", violation)

    def test_a_recording_of_another_message_is_a_harness_gap(self) -> None:
        for recorded in ("garbled", ["group/update"], ACTIVATE, {"payload": GROUP}):
            with self.subTest(recorded=recorded):
                violation = _violation(_group_update(**GROUP), recorded)

                self.assertIsNotNone(violation)
                self.assertTrue(violation.startswith(HARNESS_GAP), violation)

    def test_a_group_update_without_a_payload_object_is_a_server_defect(self) -> None:
        for recorded in ({"type": "group/update"}, {"type": "group/update", "payload": ["stopped"]}):
            with self.subTest(recorded=recorded):
                violation = _violation(recorded)

                self.assertIsNotNone(violation)
                self.assertTrue(violation.startswith("Server's group/update 1 of 1"), violation)
                self.assertIn("no payload object", violation)

    def test_a_server_that_sent_no_activation_is_left_to_that_verdict(self) -> None:
        for summary in ({"activation": None}, {"activation": None, "group_updates": []}):
            with self.subTest(summary=summary):
                self.assertIsNone(group_update_violation(summary))


class CaseVerdictTest(unittest.TestCase):
    """The rule as it reaches a case result."""

    scenario = require_scenario("server-initiated-pcm")

    def _verdict(self, **server_extra: Any) -> tuple[bool, str]:
        return _compare_summaries(
            self.scenario,
            _audio_summary(
                "server",
                activation=ACTIVATE,
                availability_trace=AVAILABILITY_TRACE,
                time_exchange=[],
                **server_extra,
            ),
            _audio_summary("client"),
        )

    def test_a_case_with_a_conformant_group_update_passes(self) -> None:
        matches, _ = self._verdict(group_updates=[_group_update(**GROUP)])

        self.assertTrue(matches)

    def test_a_case_that_otherwise_passes_fails_without_a_group_update(self) -> None:
        matches, reason = self._verdict(group_updates=[])

        self.assertFalse(matches)
        self.assertIn("Server sent no group/update", reason)

    def test_a_case_fails_on_a_later_update_omitting_a_field(self) -> None:
        matches, reason = self._verdict(group_updates=[_group_update(**GROUP), _without("group_name")])

        self.assertFalse(matches)
        self.assertIn("omitted group_name", reason)

    def test_a_case_whose_server_recorded_nothing_never_passes(self) -> None:
        matches, reason = self._verdict()

        self.assertFalse(matches)
        self.assertTrue(reason.startswith(HARNESS_GAP), reason)

    def test_a_missing_activation_is_reported_first(self) -> None:
        matches, reason = _compare_summaries(
            self.scenario,
            _audio_summary("server", activation=None, group_updates=[]),
            _audio_summary("client"),
        )

        self.assertFalse(matches)
        self.assertIn("Server sent no initial server/activate", reason)

    def test_the_scenarios_own_failure_is_reported_first(self) -> None:
        matches, reason = _compare_summaries(
            self.scenario,
            _audio_summary("server", activation=ACTIVATE, group_updates=[]),
            {
                **_audio_summary("client"),
                "audio": {"received_pcm_sha256": "b" * 64, "audio_chunk_count": 4},
            },
        )

        self.assertFalse(matches)
        self.assertNotIn("group/update", reason)

    def test_an_adapter_reported_failure_is_reported_first(self) -> None:
        matches, reason = _compare_summaries(
            self.scenario,
            {"status": "error", "reason": "handshake failed"},
            _audio_summary("client"),
        )

        self.assertFalse(matches)
        self.assertEqual(reason, "Server adapter reported: handshake failed")


if __name__ == "__main__":
    unittest.main()
