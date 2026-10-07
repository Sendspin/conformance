"""
Coverage for the verdict drawn from a server's recorded initial ``server/activate``.

The sendspin-go server sends none in the published matrix and needs a Go
toolchain to run, and no implementation puts an activity RC1 does not
define on the wire today. The rule is therefore exercised here against
synthetic summaries shaped like the ones the adapters really write.
"""

from __future__ import annotations

import unittest
from typing import Any

from conformance.protocol import activation_violation
from conformance.runner import _compare_summaries
from conformance.scenarios import require_scenario

PCM_HASH = "a" * 64

OPUS = {"codec": "opus", "sample_rate": 8000, "bit_depth": 16, "channels": 1}
PCM = {"codec": "pcm", "sample_rate": 8000, "bit_depth": 16, "channels": 1}


def _activate(**payload: Any) -> dict[str, Any]:
    return {"type": "server/activate", "payload": payload}


PLAYBACK_ACTIVATE = _activate(activities=["playback"], active_roles=["player@v1"])


# What a server that held stream/start until the client reported available records.
AVAILABILITY_TRACE = [
    {"type": "client/state", "available": True},
    {"type": "stream/start", "phase": "sending", "roles": ["player"]},
    {"type": "stream/start", "phase": "sent", "roles": ["player"]},
]


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


class ActivationViolationTest(unittest.TestCase):
    """The rule itself, independent of any scenario's own verification."""

    def test_no_activation_sent_is_reported_as_a_server_defect(self) -> None:
        violation = activation_violation({"activation": None})

        assert violation is not None
        self.assertTrue(violation.startswith("Server sent no initial server/activate"))
        self.assertNotIn("client", violation.casefold())

    def test_conformant_activations_are_not_reported(self) -> None:
        for activation in (
            PLAYBACK_ACTIVATE,
            _activate(activities=[], active_roles=[]),
            _activate(activities=["pairing"], active_roles=[], pairing={"method": "pairing_psk"}),
            _activate(activities=["playback", "pairing"], active_roles=["player@v1"]),
        ):
            with self.subTest(activation=activation):
                self.assertIsNone(activation_violation({"activation": activation}))

    def test_activities_rc1_does_not_define_or_allow_are_reported(self) -> None:
        for activities in (
            ["management"],
            ["playback", "management"],
            ["playback", "playback"],
            ["Playback"],
            [{"name": "playback"}],
            "playback",
            None,
        ):
            with self.subTest(activities=activities):
                violation = activation_violation(
                    {"activation": _activate(activities=activities, active_roles=[])}
                )

                assert violation is not None
                self.assertTrue(violation.startswith("Server's initial server/activate declared"))
                self.assertIn("'pairing' and 'playback'", violation)

    def test_the_reason_quotes_the_activities_as_sent(self) -> None:
        violation = activation_violation(
            {"activation": _activate(activities=["playback", "management"], active_roles=[])}
        )

        assert violation is not None
        self.assertIn('["playback", "management"]', violation)

    def test_missing_activities_is_reported(self) -> None:
        violation = activation_violation({"activation": _activate(active_roles=[])})

        assert violation is not None
        self.assertIn("omitted activities", violation)

    def test_missing_active_roles_is_reported(self) -> None:
        violation = activation_violation({"activation": _activate(activities=["playback"])})

        assert violation is not None
        self.assertTrue(violation.startswith("Server's initial server/activate omitted"))
        self.assertIn("active_roles", violation)
        self.assertNotIn("declared activities", violation)

    def test_every_defect_in_one_activation_is_named(self) -> None:
        violation = activation_violation({"activation": _activate(activities=["management"])})

        assert violation is not None
        self.assertIn("declared activities", violation)
        self.assertIn("; omitted active_roles", violation)

    def test_the_psk_dependent_rules_are_not_judged(self) -> None:
        # Whether these are admissible depends on which PSK matched, which no
        # summary records.
        for activation in (
            _activate(activities=["pairing"], active_roles=["player@v1"]),
            _activate(activities=[], active_roles=["player@v1"]),
            _activate(activities=["pairing"], active_roles=[]),
        ):
            with self.subTest(activation=activation):
                self.assertIsNone(activation_violation({"activation": activation}))

    def test_only_the_presence_of_active_roles_is_judged(self) -> None:
        for active_roles in (None, "player@v1"):
            with self.subTest(active_roles=active_roles):
                self.assertIsNone(
                    activation_violation(
                        {"activation": _activate(activities=[], active_roles=active_roles)}
                    )
                )

    def test_no_reason_can_be_read_as_a_client_fault_or_a_capability_gap(self) -> None:
        for activation in (
            None,
            _activate(),
            _activate(activities=["management"]),
            _activate(activities=["playback"]),
        ):
            with self.subTest(activation=activation):
                violation = activation_violation({"activation": activation})

                assert violation is not None
                self.assertTrue(violation.startswith("Server"))
                self.assertNotIn("client", violation.casefold())
                # The report styles a reason carrying this phrase as a capability gap.
                self.assertNotIn("does not support", violation)

    def test_a_summary_without_evidence_is_not_judged(self) -> None:
        for summary in (
            {},
            {"activation": "garbled"},
            {"activation": {"type": "server/hello", "payload": {}}},
            {"activation": {"type": "server/activate"}},
            {"activation": {"type": "server/activate", "payload": ["playback"]}},
        ):
            with self.subTest(summary=summary):
                self.assertIsNone(activation_violation(summary))


class CaseVerdictTest(unittest.TestCase):
    """The rule as it reaches a case result."""

    scenario = require_scenario("server-initiated-pcm")

    def _verdict(self, **server_extra: Any) -> tuple[bool, str]:
        return _compare_summaries(
            self.scenario,
            _audio_summary("server", availability_trace=AVAILABILITY_TRACE, **server_extra),
            _audio_summary("client"),
        )

    def test_a_case_that_otherwise_passes_fails_without_an_activation(self) -> None:
        matches, reason = self._verdict(activation=None)

        self.assertFalse(matches)
        self.assertIn("Server sent no initial server/activate", reason)

    def test_a_case_fails_on_an_activity_rc1_does_not_define(self) -> None:
        matches, reason = self._verdict(
            activation=_activate(activities=["management"], active_roles=[])
        )

        self.assertFalse(matches)
        self.assertIn('["management"]', reason)

    def test_a_case_with_a_conformant_activation_passes(self) -> None:
        matches, _ = self._verdict(activation=PLAYBACK_ACTIVATE)

        self.assertTrue(matches)

    def test_a_case_whose_server_recorded_nothing_is_not_failed(self) -> None:
        matches, _ = self._verdict()

        self.assertTrue(matches)

    def test_the_scenarios_own_failure_is_reported_first(self) -> None:
        matches, reason = _compare_summaries(
            self.scenario,
            _audio_summary("server", activation=None),
            {
                **_audio_summary("client"),
                "audio": {"received_pcm_sha256": "b" * 64, "audio_chunk_count": 4},
            },
        )

        self.assertFalse(matches)
        self.assertNotIn("server/activate", reason)

    def test_an_adapter_reported_failure_is_reported_first(self) -> None:
        matches, reason = _compare_summaries(
            self.scenario,
            {"status": "error", "reason": "handshake failed", "activation": None},
            _audio_summary("client"),
        )

        self.assertFalse(matches)
        self.assertEqual(reason, "Server adapter reported: handshake failed")


class FormatPriorityPrecedenceTest(unittest.TestCase):
    """Neither verdict the opus scenario carries may mask the other."""

    scenario = require_scenario("server-initiated-opus")

    def _verdict(self, declared: list[dict[str, Any]]) -> tuple[bool, str]:
        encoded = {"sent_audio_chunk_count": 4, "sent_encoded_sha256": "b" * 64}
        return _compare_summaries(
            self.scenario,
            {
                "status": "ok",
                "activation": None,
                "peer_hello": {
                    "type": "client/hello",
                    "payload": {"player@v1_support": {"supported_formats": declared}},
                },
                "stream": OPUS,
                "audio": encoded,
            },
            {
                "status": "ok",
                "stream": OPUS,
                "audio": {"audio_chunk_count": 4, "received_encoded_sha256": "b" * 64},
            },
        )

    def test_a_bad_format_priority_keeps_its_own_reason(self) -> None:
        matches, reason = self._verdict([PCM, OPUS])

        self.assertFalse(matches)
        self.assertIn("did not list OPUS first", reason)

    def test_a_sound_format_priority_leaves_the_activation_reason(self) -> None:
        matches, reason = self._verdict([OPUS, PCM])

        self.assertFalse(matches)
        self.assertIn("Server sent no initial server/activate", reason)


if __name__ == "__main__":
    unittest.main()
