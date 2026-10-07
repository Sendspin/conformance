"""Coverage for the spec MUST on the first `server/state` that carries metadata.

`messaging.md` requires that first state to carry a past or present
`timestamp`, so a client is brought up to date before any scheduled update
follows. Reproducing a violation needs a server built to schedule its first
state, which no implementation in the matrix does, so the rule is exercised
here against synthetic summaries shaped like the ones the adapters really
write.
"""

from __future__ import annotations

import unittest
from typing import Any

from conformance.runner import _compare_summaries, _first_metadata_state_verdict
from conformance.scenarios import require_scenario

SNAPSHOT: dict[str, Any] = {
    "title": "Almost Silent",
    "artist": "Sendspin Conformance",
    "album_artist": "Sendspin",
    "album": "Protocol Fixtures",
    "artwork_url": "https://example.invalid/almost-silent.jpg",
    "year": 2026,
    "track": 1,
    "progress": {
        "track_progress": 12_000,
        "track_duration": 180_000,
        "playback_speed": 1_000,
    },
}

# The stamp the server recorded sending, and the clock reading it took once that
# frame was on the wire. The real margin measures in tens of microseconds.
SENT_STAMP_US = 1_700_000_000_000_000
SENT_BOUND_US = SENT_STAMP_US + 40

# A server that stamped its first state three seconds ahead of sending it.
SCHEDULED_STAMP_US = SENT_BOUND_US + 3_000_000


# Every server summary carries one, and a case whose summary lacks it never passes.
GROUP_UPDATE = {
    "type": "group/update",
    "payload": {"playback_state": "stopped", "group_id": "group-1", "group_name": "Kitchen"},
}


def _server_summary(*, sent: Any = None, include_sent: bool = True) -> dict[str, Any]:
    metadata: dict[str, Any] = {"expected": SNAPSHOT}
    if include_sent:
        metadata["first_state_sent"] = (
            {"timestamp_us": SENT_STAMP_US, "bound_us": SENT_BOUND_US}
            if sent is None
            else sent
        )
    return {
        "status": "ok",
        "implementation": "synthetic-server",
        "role": "server",
        "group_update": GROUP_UPDATE,
        "metadata": metadata,
    }


def _scheduled_server_summary() -> dict[str, Any]:
    """A server whose first state went out stamped three seconds in the future."""
    return _server_summary(
        sent={"timestamp_us": SCHEDULED_STAMP_US, "bound_us": SENT_BOUND_US}
    )


def _client_summary(
    *,
    first_object_state: Any = None,
    include_first_object_state: bool = True,
    update_count: int = 2,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "update_count": update_count,
        "received": SNAPSHOT,
    }
    if include_first_object_state:
        metadata["first_object_state"] = first_object_state
    return {
        "status": "ok",
        "implementation": "synthetic-client",
        "role": "client",
        "metadata": metadata,
    }


def _observed(timestamp_us: Any, *, update_index: int = 2) -> dict[str, Any]:
    return {"update_index": update_index, "timestamp_us": timestamp_us}


class FirstMetadataStateVerdictTests(unittest.TestCase):
    """The rule in isolation, over the summary shapes the adapters write."""

    def assertNotJudged(self, verdict: tuple[bool, str | None]) -> None:
        """Assert the rule found no evidence to judge, rather than judging it clean."""
        self.assertEqual((False, None), verdict)

    def assertHeld(self, verdict: tuple[bool, str | None]) -> None:
        """Assert the rule was evaluated and the state satisfied it."""
        self.assertEqual((True, None), verdict)

    def assertViolated(self, verdict: tuple[bool, str | None], expected: str) -> str:
        """Assert the rule was evaluated and name what it reported."""
        evaluated, violation = verdict
        self.assertTrue(evaluated)
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertIn(expected, violation)
        return violation

    def test_a_past_timestamp_is_accepted(self) -> None:
        self.assertHeld(
            _first_metadata_state_verdict(
                _server_summary(),
                _client_summary(first_object_state=_observed(SENT_STAMP_US)),
            )
        )

    def test_a_present_timestamp_is_accepted(self) -> None:
        # The comparison admits equality, which is what "past or present" means.
        self.assertHeld(
            _first_metadata_state_verdict(
                _server_summary(sent={"timestamp_us": SENT_STAMP_US, "bound_us": SENT_STAMP_US}),
                _client_summary(first_object_state=_observed(SENT_STAMP_US)),
            )
        )

    def test_a_state_sent_after_its_own_timestamp_elapsed_is_a_violation(self) -> None:
        # The bound belongs to the state that carried the stamp, so a stamp set
        # ahead of when that frame went out is caught however long the case ran
        # afterwards.
        violation = self.assertViolated(
            _first_metadata_state_verdict(
                _scheduled_server_summary(),
                _client_summary(first_object_state=_observed(SCHEDULED_STAMP_US)),
            ),
            "scheduled",
        )
        self.assertIn("3000.0 ms", violation)

    def test_absent_timestamp_is_a_violation(self) -> None:
        self.assertViolated(
            _first_metadata_state_verdict(
                _server_summary(),
                _client_summary(first_object_state=_observed(None)),
            ),
            "no timestamp",
        )

    def test_client_that_does_not_report_the_state_is_not_judged(self) -> None:
        self.assertNotJudged(
            _first_metadata_state_verdict(
                _server_summary(),
                _client_summary(include_first_object_state=False),
            )
        )

    def test_client_that_saw_no_metadata_object_is_not_judged(self) -> None:
        self.assertNotJudged(
            _first_metadata_state_verdict(
                _server_summary(),
                _client_summary(first_object_state=None),
            )
        )

    def test_client_that_omits_the_timestamp_field_is_not_judged(self) -> None:
        # Reporting which observation was first does not claim anything about
        # its timestamp, so it must not be read as a server that omitted one.
        self.assertNotJudged(
            _first_metadata_state_verdict(
                _server_summary(),
                _client_summary(first_object_state={"update_index": 2}),
            )
        )

    def test_server_that_recorded_no_sent_state_is_not_judged(self) -> None:
        self.assertNotJudged(
            _first_metadata_state_verdict(
                _server_summary(include_sent=False),
                _client_summary(first_object_state=_observed(SCHEDULED_STAMP_US)),
            )
        )

    def test_unreadable_bound_is_not_judged(self) -> None:
        self.assertNotJudged(
            _first_metadata_state_verdict(
                _server_summary(sent={"timestamp_us": SENT_STAMP_US, "bound_us": "later"}),
                _client_summary(first_object_state=_observed(SENT_STAMP_US)),
            )
        )

    def test_unreadable_timestamp_is_not_judged(self) -> None:
        self.assertNotJudged(
            _first_metadata_state_verdict(
                _server_summary(),
                _client_summary(first_object_state=_observed("now")),
            )
        )

    def test_a_state_the_server_did_not_record_is_not_judged(self) -> None:
        # The two sides describe the first metadata-carrying state
        # independently. If they name different timestamps they are not looking
        # at the same state, so no recorded bound belongs to the client's.
        self.assertNotJudged(
            _first_metadata_state_verdict(
                _server_summary(),
                _client_summary(first_object_state=_observed(SENT_STAMP_US + 5_000_000)),
            )
        )


class MetadataScenarioVerificationTests(unittest.TestCase):
    """The same rule through the verification the matrix actually runs."""

    def _compare(
        self,
        client_summary: dict[str, Any],
        server_summary: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        return _compare_summaries(
            require_scenario("server-initiated-metadata"),
            _server_summary() if server_summary is None else server_summary,
            client_summary,
        )

    def test_matching_snapshot_with_a_past_timestamp_passes(self) -> None:
        matches, reason = self._compare(
            _client_summary(first_object_state=_observed(SENT_STAMP_US))
        )
        self.assertTrue(matches, reason)
        self.assertIn("past or present timestamp", reason)

    def test_an_activation_state_is_judged_when_it_carries_a_timestamp(self) -> None:
        # Current aiosendspin emits a timestamp-only metadata object while the
        # role activates, so the client's first observation is the judged one.
        matches, reason = self._compare(
            _client_summary(
                first_object_state=_observed(SENT_STAMP_US, update_index=1),
                update_count=2,
            )
        )
        self.assertTrue(matches, reason)

    def test_scheduled_first_state_fails_an_otherwise_matching_case(self) -> None:
        matches, reason = self._compare(
            _client_summary(first_object_state=_observed(SCHEDULED_STAMP_US)),
            _scheduled_server_summary(),
        )
        self.assertFalse(matches)
        self.assertIn("scheduled", reason)

    def test_first_state_without_a_timestamp_fails(self) -> None:
        matches, reason = self._compare(_client_summary(first_object_state=_observed(None)))
        self.assertFalse(matches)
        self.assertIn("no timestamp", reason)

    def test_client_that_does_not_report_the_state_still_passes(self) -> None:
        matches, reason = self._compare(_client_summary(include_first_object_state=False))
        self.assertTrue(matches, reason)
        self.assertNotIn("past or present timestamp", reason)

    def test_snapshot_mismatch_is_reported_ahead_of_the_timestamp_rule(self) -> None:
        client_summary = _client_summary(first_object_state=_observed(None))
        client_summary["metadata"]["received"] = {**SNAPSHOT, "title": "Something Else"}
        matches, reason = self._compare(client_summary)
        self.assertFalse(matches)
        self.assertIn("Metadata mismatch", reason)


if __name__ == "__main__":
    unittest.main()
