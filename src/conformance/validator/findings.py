"""The verdicts the validator reaches and the findings that carry them."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Verdict(StrEnum):
    """How a check resolved."""

    PASS = "PASS"
    FAIL = "FAIL"
    """A MUST was violated, or the user reported a wrong answer."""
    WARN = "WARN"
    """A SHOULD was violated."""
    SKIPPED = "SKIPPED"
    """The user skipped the check or was not sure."""
    NOT_APPLICABLE = "N/A"
    """The client does not advertise the feature, or the session never reached it."""
    TOOL_ERROR = "TOOL_ERROR"
    """The validator itself could not observe the result."""


class Evidence(StrEnum):
    """What a verdict rests on."""

    MACHINE = "machine"
    """Frames the validator recorded."""
    HUMAN = "human"
    """An answer the user gave."""


@dataclass(frozen=True, slots=True)
class Finding:
    """
    One check's verdict on one connection.

    `id` is stable across releases, so reports can be compared over time. `spec_ref`
    cites the spec file and line at the tag the validator targets, and `frames` the
    ids of the recorded frames the verdict rests on.
    """

    id: str
    verdict: Verdict
    spec_ref: str
    message: str
    evidence: Evidence = Evidence.MACHINE
    frames: tuple[int, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """Return the finding as `report.json` records it."""
        return {
            "evidence": self.evidence.value,
            "frames": list(self.frames),
            "id": self.id,
            "message": self.message,
            "spec_ref": self.spec_ref,
            "verdict": self.verdict.value,
        }
