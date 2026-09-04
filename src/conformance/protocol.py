"""Shared protocol-conformance evidence contract and assertions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SPEC_REVISION = "8c9577ea8719ad082d051ec13cc73ef15ed68948"


@dataclass(frozen=True)
class ProtocolAssertion:
    """A normative requirement exercised by a protocol scenario."""

    id: str
    title: str
    specification_url: str


PROTOCOL_ASSERTIONS: dict[str, ProtocolAssertion] = {
    "CORE-001": ProtocolAssertion(
        id="CORE-001",
        title="Handshake messages use the required order and WebSocket frame types.",
        specification_url=(
            "https://github.com/Sendspin/spec/blob/"
            f"{SPEC_REVISION}/messaging.md#communication"
        ),
    ),
    "CORE-002": ProtocolAssertion(
        id="CORE-002",
        title="No application message is exchanged before the first server/activate.",
        specification_url=(
            "https://github.com/Sendspin/spec/blob/"
            f"{SPEC_REVISION}/messaging.md#communication"
        ),
    ),
    "CORE-003": ProtocolAssertion(
        id="CORE-003",
        title="The server activates only advertised role versions with required support objects.",
        specification_url=(
            "https://github.com/Sendspin/spec/blob/"
            f"{SPEC_REVISION}/messaging.md#client--server-clienthello"
        ),
    ),
    "CORE-004": ProtocolAssertion(
        id="CORE-004",
        title="Each activated client sends an initial client/state before role binary data.",
        specification_url=(
            "https://github.com/Sendspin/spec/blob/"
            f"{SPEC_REVISION}/messaging.md#client--server-clientstate"
        ),
    ),
    "PLAYER-001": ProtocolAssertion(
        id="PLAYER-001",
        title="A player stream selects an advertised format and sends valid timestamped chunks.",
        specification_url=(
            "https://github.com/Sendspin/spec/blob/"
            f"{SPEC_REVISION}/roles/player/v1.md#server--client-streamstart-player-object"
        ),
    ),
}


def protocol_evidence_failure(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
    *,
    assertion_ids: tuple[str, ...],
) -> str | None:
    """Return a precise missing or failed protocol-evidence reason."""
    unknown = [assertion_id for assertion_id in assertion_ids if assertion_id not in PROTOCOL_ASSERTIONS]
    if unknown:
        return f"Scenario references unknown protocol assertion(s): {', '.join(unknown)}"

    for role, summary in (("server", server_summary), ("client", client_summary)):
        protocol = summary.get("protocol")
        if not isinstance(protocol, dict):
            return f"{role.capitalize()} summary is missing required protocol evidence"
        if protocol.get("spec_revision") != SPEC_REVISION:
            return (
                f"{role.capitalize()} protocol evidence targets spec revision "
                f"{protocol.get('spec_revision')!r}, expected {SPEC_REVISION!r}"
            )
        assertions = protocol.get("assertions")
        if not isinstance(assertions, dict):
            return f"{role.capitalize()} protocol evidence is missing its assertions map"
        for assertion_id in assertion_ids:
            result = assertions.get(assertion_id)
            if not isinstance(result, dict):
                return f"{role.capitalize()} protocol evidence is missing {assertion_id}"
            if result.get("status") != "passed":
                detail = result.get("detail")
                suffix = f": {detail}" if isinstance(detail, str) and detail else ""
                return f"{role.capitalize()} did not pass {assertion_id}{suffix}"
            events = result.get("events")
            if not isinstance(events, list) or not events:
                return f"{role.capitalize()} {assertion_id} has no trace events"
    return None
