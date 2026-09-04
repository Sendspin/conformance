"""Shared dataclasses for the conformance harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


AdapterKind = Literal["python", "dotnet", "node", "cargo", "swift", "go", "cmake", "placeholder", "none"]
CaseStatus = Literal["passed", "failed", "skipped"]
InitiatorRole = Literal["server", "client"]
RoleName = Literal["server", "client"]
RoleFamily = Literal["player", "metadata", "controller", "artwork"]
VerificationMode = Literal[
    "audio-pcm",
    "audio-encoded-bytes",
    "metadata",
    "controller",
    "artwork",
    "format-renegotiation",
    "protocol",
]

# The fields that together identify one audio format on the wire. Anything
# comparing two formats must agree on this set, so it lives here rather than in
# whichever module happens to compare them.
AUDIO_FORMAT_FIELDS: tuple[str, ...] = ("codec", "sample_rate", "bit_depth", "channels")


@dataclass(frozen=True)
class RoleSpec:
    """Capabilities and launch metadata for one implementation role."""

    supported: bool
    adapter_kind: AdapterKind
    build_adapter: str | None = None
    entrypoint: str | None = None
    supports_server_initiated: bool = False
    supports_client_initiated: bool = False
    supports_flac: bool = False
    supports_opus: bool = False
    supports_discovery: bool = False
    supports_request_format: bool = False
    supported_role_families: tuple[RoleFamily, ...] = ()
    reason: str | None = None

    def supports_initiator(self, initiator_role: InitiatorRole) -> bool:
        """Return whether this role supports a scenario initiator."""
        if initiator_role == "server":
            return self.supports_server_initiated
        return self.supports_client_initiated

    def supports_codec(self, preferred_codec: str) -> bool:
        """Return whether this role supports a scenario codec."""
        if preferred_codec == "flac":
            return self.supports_flac
        if preferred_codec == "opus":
            return self.supports_opus
        return True

    def supports_role_families(self, role_families: tuple[RoleFamily, ...]) -> bool:
        """Return whether this role supports every required scenario family."""
        supported = set(self.supported_role_families)
        return all(role_family in supported for role_family in role_families)

    def unsupported_reason(
        self,
        *,
        implementation: str,
        role: RoleName,
        scenario: "ScenarioSpec",
    ) -> str | None:
        """Explain why this role cannot execute the scenario."""
        if (
            self.supports_initiator(scenario.initiator_role)
            and self.supports_codec(scenario.preferred_codec)
            and self.supports_role_families(scenario.required_role_families)
            and (self.supports_request_format or not scenario.requires_request_format)
        ):
            return None

        if scenario.initiator_role == "server":
            action = "server-initiated incoming websocket flow"
        else:
            action = "client-initiated outbound websocket flow"

        if not self.supports_initiator(scenario.initiator_role):
            return (
                f"{implementation} {role} adapter does not support the {action} "
                f"required by {scenario.id}."
            )

        if not self.supports_role_families(scenario.required_role_families):
            missing = [
                role_family
                for role_family in scenario.required_role_families
                if role_family not in set(self.supported_role_families)
            ]
            missing_display = ", ".join(sorted(missing))
            return (
                f"{implementation} {role} adapter does not support role family/families "
                f"{missing_display} required by {scenario.id}."
            )

        if scenario.requires_request_format and not self.supports_request_format:
            return (
                f"{implementation} {role} adapter does not support the "
                f"stream/request-format renegotiation required by {scenario.id}."
            )

        return (
            f"{implementation} {role} adapter does not support "
            f"{scenario.preferred_codec.upper()} transport required by {scenario.id}."
        )


@dataclass(frozen=True)
class ImplementationSpec:
    """Conformance metadata for one implementation repository."""

    name: str
    display_name: str
    repo_dirname: str
    remote_url: str
    client: RoleSpec
    server: RoleSpec


@dataclass(frozen=True)
class ScenarioSpec:
    """Describes a runnable scenario in the matrix."""

    id: str
    display_name: str
    description: str
    initiator_role: InitiatorRole
    preferred_codec: str
    required_role_families: tuple[RoleFamily, ...]
    verification_mode: VerificationMode
    requires_request_format: bool = False
    extra_cli_args: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    protocol_assertions: tuple[str, ...] = field(default_factory=tuple)

    def cli_args(self) -> dict[str, str]:
        """Return scenario-wide CLI arguments passed to both roles."""
        return {
            "scenario_id": self.id,
            "initiator_role": self.initiator_role,
            "preferred_codec": self.preferred_codec,
            **dict(self.extra_cli_args),
        }


@dataclass
class CaseResult:
    """A single scenario result for one server/client pair."""

    environment_id: str
    environment_name: str
    scenario_id: str
    server_impl: str
    client_impl: str
    status: CaseStatus
    reason: str
    case_dir: str
    server_exit_code: int | None = None
    client_exit_code: int | None = None
