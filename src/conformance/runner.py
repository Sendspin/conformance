"""Matrix runner for Sendspin conformance cases."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import shutil
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .declared_formats import undeclared_format_violation
from .environment import resolve_environment
from .fixtures import fixture_path
from .implementations import (
    IMPLEMENTATIONS,
    ensure_repo_checkout,
    implementations_for_scenario,
    parse_implementation_filter,
    resolve_repo_path,
)
from .io import read_json, write_json
from .models import AUDIO_FORMAT_FIELDS, CaseResult, RoleName, ScenarioSpec
from .paths import repo_root
from .process import close_process_log, collect_process, wait_for_exit, wait_for_file
from .protocol import protocol_evidence_failure
from .scenarios import ordered_scenarios, require_scenario
from .toolchains import find_cargo, find_cmake, find_dotnet, find_go, find_swift

SERVER_PORT_BASE = 18927
CLIENT_PORT_BASE = 19927

# Codecs whose decoded output is sample-identical to what was encoded, so a
# decoded stream can be hash-compared with its source.
_LOSSLESS_CODECS = frozenset({"flac"})

# "error" as a word of its own, so a dependency named quick-error or thiserror is not one.
_BUILD_ERROR_LINE = re.compile(r"(?<![\w-])error(?![\w-])", re.IGNORECASE)


def _case_resource_keys(
    *,
    scenario_id: str,
    server_impl: str,
    client_impl: str,
) -> tuple[str, ...]:
    """Return shared resources that make a case incompatible with parallel peers."""
    keys: list[str] = []
    if client_impl == "sendspin-go":
        # The Go adapter is stable under the matrix, but local parallel runs can stall before
        # the paired server writes its ready file. Serialize Go client cases to match CI.
        keys.append("sendspin-go-client-runtime")
    return tuple(keys)


@dataclass(frozen=True)
class LaunchSpec:
    """Process launch metadata for one adapter invocation."""

    cmd: list[str]
    cwd: Path


def _command_with_args(prefix: list[str], **kwargs: str) -> list[str]:
    cmd = [*prefix]
    for key, value in kwargs.items():
        cmd.extend([f"--{key.replace('_', '-')}", value])
    return cmd


def _python_adapter_command(module: str, **kwargs: str) -> LaunchSpec:
    return LaunchSpec(
        cmd=_command_with_args([sys.executable, "-m", module], **kwargs),
        cwd=repo_root(),
    )


def _runtime_command_prefix(build_result: dict[str, Any] | None) -> list[str] | None:
    if build_result is None:
        return None
    runtime = build_result.get("runtime_command_prefix")
    if not isinstance(runtime, list) or not runtime:
        return None
    if not all(isinstance(part, str) for part in runtime):
        return None
    return [str(part) for part in runtime]


def _dotnet_adapter_command(
    project: str,
    *,
    build_result: dict[str, Any] | None = None,
    **kwargs: str,
) -> LaunchSpec | None:
    runtime_prefix = _runtime_command_prefix(build_result)
    if runtime_prefix is not None:
        return LaunchSpec(
            cmd=_command_with_args(runtime_prefix, **kwargs),
            cwd=repo_root(),
        )
    if build_result is not None:
        return None
    dotnet = find_dotnet()
    if dotnet is None:
        return None
    return LaunchSpec(
        cmd=_command_with_args(
            [dotnet, "run", "--no-build", "--project", project, "--"],
            **kwargs,
        ),
        cwd=repo_root(),
    )


def _node_adapter_command(script: str, **kwargs: str) -> LaunchSpec | None:
    node = shutil.which("node")
    if node is None:
        return None
    return LaunchSpec(
        cmd=_command_with_args([node, script], **kwargs),
        cwd=repo_root(),
    )


def _cargo_adapter_command(
    manifest: str,
    *,
    build_result: dict[str, Any] | None = None,
    **kwargs: str,
) -> LaunchSpec | None:
    runtime_prefix = _runtime_command_prefix(build_result)
    if runtime_prefix is not None:
        return LaunchSpec(
            cmd=_command_with_args(runtime_prefix, **kwargs),
            cwd=repo_root(),
        )
    if build_result is not None:
        return None
    cargo = find_cargo()
    if cargo is None:
        return None
    return LaunchSpec(
        cmd=_command_with_args(
            [cargo, "run", "--quiet", "--manifest-path", manifest, "--"],
            **kwargs,
        ),
        cwd=repo_root(),
    )


def _swift_adapter_command(
    package_path: str,
    product: str,
    *,
    build_result: dict[str, Any] | None = None,
    **kwargs: str,
) -> LaunchSpec | None:
    runtime_prefix = _runtime_command_prefix(build_result)
    if runtime_prefix is not None:
        return LaunchSpec(
            cmd=_command_with_args(runtime_prefix, **kwargs),
            cwd=repo_root(),
        )
    if build_result is not None:
        return None
    swift = find_swift()
    if swift is None:
        return None
    return LaunchSpec(
        cmd=_command_with_args(
            [swift, "run", "--package-path", package_path, product, "--"],
            **kwargs,
        ),
        cwd=repo_root(),
    )


def _cmake_adapter_command(
    adapter_dir: str,
    *,
    build_result: dict[str, Any] | None = None,
    **kwargs: str,
) -> LaunchSpec | None:
    runtime_prefix = _runtime_command_prefix(build_result)
    if runtime_prefix is not None:
        return LaunchSpec(
            cmd=_command_with_args(runtime_prefix, **kwargs),
            cwd=repo_root(),
        )
    if build_result is not None:
        return None
    cmake = find_cmake()
    if cmake is None:
        return None
    build_dir = repo_root() / adapter_dir / "build"
    binary = build_dir / "conformance-sendspin-cpp-client"
    if not binary.exists():
        return None
    return LaunchSpec(
        cmd=_command_with_args([str(binary)], **kwargs),
        cwd=repo_root(),
    )


def _go_adapter_command(
    package_path: str,
    *,
    build_result: dict[str, Any] | None = None,
    **kwargs: str,
) -> LaunchSpec | None:
    runtime_prefix = _runtime_command_prefix(build_result)
    if runtime_prefix is not None:
        return LaunchSpec(
            cmd=_command_with_args(runtime_prefix, **kwargs),
            cwd=repo_root(),
        )
    if build_result is not None:
        return None
    go = find_go()
    if go is None:
        return None
    adapter_root = repo_root() / "adapters" / "sendspin-go"
    return LaunchSpec(
        cmd=_command_with_args([go, "run", f"./{Path(package_path).name}"], **kwargs),
        cwd=adapter_root,
    )


def _build_role_command(
    implementation: str,
    role: RoleName,
    *,
    summary: Path,
    ready: Path,
    registry: Path,
    extra_args: dict[str, str],
    build_result: dict[str, Any] | None = None,
) -> LaunchSpec | None:
    spec = IMPLEMENTATIONS[implementation]
    role_spec = spec.server if role == "server" else spec.client
    if role_spec.adapter_kind == "python":
        assert role_spec.entrypoint is not None
        return _python_adapter_command(
            role_spec.entrypoint,
            summary=str(summary),
            ready=str(ready),
            registry=str(registry),
            **extra_args,
        )
    if role_spec.adapter_kind == "dotnet":
        assert role_spec.entrypoint is not None
        return _dotnet_adapter_command(
            str(repo_root() / role_spec.entrypoint),
            build_result=build_result,
            summary=str(summary),
            ready=str(ready),
            registry=str(registry),
            **extra_args,
        )
    if role_spec.adapter_kind == "node":
        assert role_spec.entrypoint is not None
        return _node_adapter_command(
            str(repo_root() / role_spec.entrypoint),
            summary=str(summary),
            ready=str(ready),
            registry=str(registry),
            **extra_args,
        )
    if role_spec.adapter_kind == "cargo":
        assert role_spec.entrypoint is not None
        ensure_repo_checkout("sendspin-rs")
        return _cargo_adapter_command(
            str(repo_root() / role_spec.entrypoint),
            build_result=build_result,
            summary=str(summary),
            ready=str(ready),
            registry=str(registry),
            **extra_args,
        )
    if role_spec.adapter_kind == "swift":
        assert role_spec.entrypoint is not None
        ensure_repo_checkout("SendspinKit")
        package_path, product = role_spec.entrypoint.rsplit(":", 1)
        return _swift_adapter_command(
            str(repo_root() / package_path),
            product,
            build_result=build_result,
            summary=str(summary),
            ready=str(ready),
            registry=str(registry),
            **extra_args,
        )
    if role_spec.adapter_kind == "cmake":
        assert role_spec.entrypoint is not None
        ensure_repo_checkout("sendspin-cpp")
        return _cmake_adapter_command(
            role_spec.entrypoint,
            build_result=build_result,
            summary=str(summary),
            ready=str(ready),
            registry=str(registry),
            **extra_args,
        )
    if role_spec.adapter_kind == "go":
        assert role_spec.entrypoint is not None
        ensure_repo_checkout("sendspin-go")
        return _go_adapter_command(
            role_spec.entrypoint,
            build_result=build_result,
            summary=str(summary),
            ready=str(ready),
            registry=str(registry),
            **extra_args,
        )
    if role_spec.adapter_kind == "placeholder":
        assert role_spec.entrypoint is not None
        assert role_spec.reason is not None
        return _python_adapter_command(
            role_spec.entrypoint,
            summary=str(summary),
            ready=str(ready),
            registry=str(registry),
            implementation=implementation,
            role=role,
            failure_reason=role_spec.reason,
            **extra_args,
        )
    return None


@dataclass(frozen=True)
class CaseContext:
    """Shared metadata, artifacts, and CLI args for one matrix case."""

    results_dir: Path
    environment_id: str
    environment_name: str
    scenario: ScenarioSpec
    server_impl: str
    client_impl: str
    timeout_s: float
    slot_index: int

    @property
    def case_name(self) -> str:
        return (
            f"{self.environment_id}__{self.scenario.id}"
            f"__{self.server_impl}__to__{self.client_impl}"
        )

    @property
    def case_dir(self) -> Path:
        return self.results_dir / self.case_name

    @property
    def registry_path(self) -> Path:
        return self.case_dir / "registry.json"

    @property
    def server_name(self) -> str:
        return f"{self.server_impl} server"

    @property
    def server_id(self) -> str:
        return f"{self.server_impl}-server"

    @property
    def client_name(self) -> str:
        return f"{self.client_impl}-client"

    @property
    def client_id(self) -> str:
        return f"{self.client_impl}-client-id"

    @property
    def server_port(self) -> int:
        return SERVER_PORT_BASE + self.slot_index

    @property
    def client_port(self) -> int:
        return CLIENT_PORT_BASE + self.slot_index

    def summary_path(self, role: RoleName) -> Path:
        return self.case_dir / f"{role}-summary.json"

    def ready_path(self, role: RoleName) -> Path:
        return self.case_dir / f"{role}-ready.json"

    def log_path(self, role: RoleName) -> Path:
        return self.case_dir / f"{role}.log"

    def implementation(self, role: RoleName) -> str:
        if role == "server":
            return self.server_impl
        return self.client_impl

    def role_spec(self, role: RoleName):
        implementation = IMPLEMENTATIONS[self.implementation(role)]
        if role == "server":
            return implementation.server
        return implementation.client

    def role_args(self, role: RoleName) -> dict[str, str]:
        common = {
            **self.scenario.cli_args(),
            "timeout_seconds": str(self.timeout_s),
        }
        if role == "server":
            return {
                **common,
                "client_name": self.client_name,
                "client_id": self.client_id,
                "fixture": str(fixture_path()),
                "server_id": self.server_id,
                "server_name": self.server_name,
                "port": str(self.server_port),
                **(
                    {"allow_unencrypted": "true"}
                    if self.scenario.requires_legacy_unencrypted
                    else {}
                ),
            }
        args = {
            **common,
            "client_name": self.client_name,
            "client_id": self.client_id,
            "server_id": self.server_id,
            "server_name": self.server_name,
        }
        if self.role_spec(role).supports_server_initiated:
            args["port"] = str(self.client_port)
            args["path"] = "/sendspin"
        return args

    def capability_failure(self, role: RoleName) -> str | None:
        return self.role_spec(role).unsupported_reason(
            implementation=self.implementation(role),
            role=role,
            scenario=self.scenario,
        )


def _write_result(context: CaseContext, result: CaseResult) -> CaseResult:
    write_json(context.case_dir / "result.json", result.__dict__)
    return result


def _case_result(
    context: CaseContext,
    *,
    status: str,
    reason: str,
    server_exit_code: int | None = None,
    client_exit_code: int | None = None,
) -> CaseResult:
    return CaseResult(
        environment_id=context.environment_id,
        environment_name=context.environment_name,
        scenario_id=context.scenario.id,
        server_impl=context.server_impl,
        client_impl=context.client_impl,
        status=status,
        reason=reason,
        scenario_revision=context.scenario.scenario_revision,
        case_dir=str(context.case_dir),
        server_exit_code=server_exit_code,
        client_exit_code=client_exit_code,
    )


def _missing_summary_reason(
    *,
    context: CaseContext,
    server_process: asyncio.subprocess.Process,
    client_process: asyncio.subprocess.Process | None,
) -> str:
    missing_roles: list[str] = []
    if not context.summary_path("server").exists():
        missing_roles.append("server")
    if not context.summary_path("client").exists():
        missing_roles.append("client")

    details: list[str] = []
    if "client" in missing_roles and client_process is not None:
        if server_process.returncode == 0 and client_process.returncode == -9:
            details.append(
                "client adapter was killed after the server completed before it wrote a summary"
            )
        elif client_process.returncode is not None:
            details.append(
                f"client adapter exited {client_process.returncode} before writing a summary"
            )
    if "server" in missing_roles and server_process.returncode is not None:
        details.append(
            f"server adapter exited {server_process.returncode} before writing a summary"
        )

    role_text = ", ".join(missing_roles) if missing_roles else "unknown"
    if details:
        return f"Missing summary output for {role_text}: {'; '.join(details)}"
    return f"Missing summary output for {role_text}"


def _written_summaries(context: CaseContext) -> dict[RoleName, dict[str, Any]]:
    """Return the summaries the adapters left on disk, server first."""
    summaries: dict[RoleName, dict[str, Any]] = {}
    for role in ("server", "client"):
        try:
            payload = read_json(context.summary_path(role))
        except (OSError, ValueError):
            # Absent, or truncated by an adapter killed mid-write.
            continue
        if isinstance(payload, dict):
            summaries[role] = payload
    return summaries


def _adapter_reported_failures(summaries: dict[RoleName, dict[str, Any]]) -> str | None:
    """Return the reasons the adapters gave for their non-ok summaries, or None."""
    reported = [
        f"{role.capitalize()} adapter reported: {summary['reason']}"
        for role, summary in summaries.items()
        if summary.get("status") != "ok" and summary.get("reason")
    ]
    return "; ".join(reported) or None


def _incomplete_case_reason(context: CaseContext, observed: str) -> str:
    """Explain a case that ended without a usable summary from both adapters.

    An adapter that already reported why it failed leads, because what the
    harness observed is usually only the consequence of it.
    """
    reported = _adapter_reported_failures(_written_summaries(context))
    return f"{reported}; {observed}" if reported else observed


def _build_result_index(
    build_results: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    if not build_results:
        return {}
    return {
        str(result["adapter"]): result
        for result in build_results
        if isinstance(result, dict) and "adapter" in result
    }


def _role_build_result(
    context: CaseContext,
    role: RoleName,
    *,
    build_index: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    build_adapter = context.role_spec(role).build_adapter
    if build_adapter is None:
        return None
    return build_index.get(build_adapter)


def _build_failure_reason(build_result: dict[str, Any]) -> str:
    adapter = str(build_result.get("adapter") or "adapter")
    status = str(build_result.get("status") or "failed")
    detail = str(build_result.get("detail") or "").strip()
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    # The detail is the tail of the build log, so its first line is often a
    # dependency being compiled or a line cut mid-word; lead with a diagnostic.
    headline = next(
        (line for line in lines if _BUILD_ERROR_LINE.search(line)),
        lines[0] if lines else "no detail available",
    )
    return f"{adapter} build {status}: {headline}"


def _compare_audio_summaries(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
) -> tuple[bool, str]:
    if server_summary.get("status") != "ok":
        return False, f"Server summary status is {server_summary.get('status')!r}"
    if client_summary.get("status") != "ok":
        return False, f"Client summary status is {client_summary.get('status')!r}"

    server_audio = server_summary.get("audio")
    if not isinstance(server_audio, dict):
        return False, "Server summary is missing an 'audio' section"
    client_audio = client_summary.get("audio")
    if not isinstance(client_audio, dict):
        return False, "Client summary is missing an 'audio' section"

    source_hash = server_audio["source_pcm_sha256"]
    received_hash = client_audio["received_pcm_sha256"]
    if not client_audio["audio_chunk_count"]:
        return False, "Client summary shows zero audio chunks"
    if source_hash == received_hash:
        return True, "PCM hashes match exactly"

    return (
        False,
        f"PCM hash mismatch: {_sample_count_detail(server_audio, client_audio)}"
        f"server={source_hash} client={received_hash}",
    )


def _sample_count_detail(server_audio: dict[str, Any], client_audio: dict[str, Any]) -> str:
    """Return how the sent and received sample counts relate, or "" when unreported."""
    frame_count = server_audio.get("frame_count")
    channels = server_audio.get("channels")
    received = client_audio.get("received_sample_count")
    if not all(type(value) is int for value in (frame_count, channels, received)):
        return ""
    sent = frame_count * channels
    if received == sent:
        return f"sample counts equal ({sent}), content differs; "
    direction = "fewer" if received < sent else "more"
    return f"client received {received} of {sent} samples ({abs(sent - received)} {direction}); "


def _stream_codec(summary: dict[str, Any]) -> str | None:
    stream = summary.get("stream")
    if not isinstance(stream, dict):
        return None
    codec = stream.get("codec")
    return str(codec) if codec is not None else None


def _stream_codec_header_sha256(summary: dict[str, Any]) -> str | None:
    stream = summary.get("stream")
    if not isinstance(stream, dict):
        return None
    direct_hash = stream.get("codec_header_sha256")
    if isinstance(direct_hash, str) and direct_hash:
        return direct_hash
    codec_header = stream.get("codec_header")
    if not isinstance(codec_header, str) or not codec_header:
        return None
    try:
        return hashlib.sha256(base64.b64decode(codec_header)).hexdigest()
    except Exception:
        return None


def _compare_encoded_audio_summaries(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
    *,
    expected_codec: str,
) -> tuple[bool, str]:
    if server_summary.get("status") != "ok":
        return False, f"Server summary status is {server_summary.get('status')!r}"
    if client_summary.get("status") != "ok":
        return False, f"Client summary status is {client_summary.get('status')!r}"

    codec_label = expected_codec.upper()
    server_codec = _stream_codec(server_summary)
    client_codec = _stream_codec(client_summary)
    if server_codec != expected_codec:
        return (
            False,
            f"Server did not negotiate {codec_label} transport (codec={server_codec!r})",
        )
    if client_codec is not None and client_codec != expected_codec:
        return (
            False,
            f"Client did not observe {codec_label} transport (codec={client_codec!r})",
        )

    server_audio = server_summary.get("audio", {})
    client_audio = client_summary.get("audio", {})
    sent_chunk_count = int(server_audio.get("sent_audio_chunk_count") or 0)
    received_chunk_count = int(client_audio.get("audio_chunk_count") or 0)
    if sent_chunk_count <= 0:
        return False, f"Server summary shows zero {codec_label} audio chunks sent"
    if received_chunk_count <= 0:
        return False, f"Client summary shows zero {codec_label} audio chunks received"

    sent_hash = server_audio.get("sent_encoded_sha256")
    received_hash = client_audio.get("received_encoded_sha256")
    if not isinstance(sent_hash, str) or not sent_hash:
        return False, f"Server summary is missing sent {codec_label} chunk hash"
    if not isinstance(received_hash, str) or not received_hash:
        return False, f"Client summary is missing received {codec_label} chunk hash"
    if sent_hash != received_hash:
        return (
            False,
            f"{codec_label} chunk hash mismatch: "
            f"server={sent_hash} client={received_hash}",
        )

    server_header_hash = (
        server_audio.get("sent_codec_header_sha256")
        if isinstance(server_audio.get("sent_codec_header_sha256"), str)
        else _stream_codec_header_sha256(server_summary)
    )
    client_header_hash = _stream_codec_header_sha256(client_summary)
    if server_header_hash and client_header_hash and server_header_hash != client_header_hash:
        return (
            False,
            f"{codec_label} codec header mismatch: "
            f"server={server_header_hash} client={client_header_hash}",
        )
    transported = (
        "header and chunk bytes" if server_header_hash and client_header_hash else "chunk bytes"
    )

    violation, unjudged = _decoded_audio_verdict(
        server_summary,
        client_summary,
        expected_codec=expected_codec,
    )
    if violation is not None:
        return False, violation
    if unjudged is not None:
        return True, (
            f"{codec_label} {transported} match exactly; "
            f"decoded audio not judged against the source clip: {unjudged}"
        )
    return True, (
        f"{codec_label} {transported} match exactly and the decoded audio matches the source clip"
    )


def _decoded_audio_verdict(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
    *,
    expected_codec: str,
) -> tuple[str | None, str | None]:
    """
    Judge the audio the client decoded against the source clip, as (violation, unjudged).

    `violation` is why the decoded audio is not the source clip. `unjudged` is
    why the rule could not be applied to this case. Both are None when the rule
    ran and held, and at most one is set.

    Matching sent bytes against received bytes proves only that the transport
    delivered what the server sent. A server that sends part of the clip still
    satisfies it, because the client really did receive everything that went
    out. Comparing what the client decoded with the clip the server started
    from is what catches the missing audio.

    The rule is judged only where a hash equality can hold and the evidence to
    test it exists:

    - A lossy codec never decodes back to its source, so it is not judged.
    - A client adapter that does not decode the stream reports no PCM hash, or
      a hash over zero samples, and is not judged. A client whose decoder
      produced nothing from a stream it was meant to decode is indistinguishable
      from that and is not judged either.
    - A stream negotiated at another sample rate or channel count than the
      source was resampled or remixed on the way, so it is not judged. Bit depth
      is deliberately left out: the hash is taken over canonical float samples,
      which a widening such as 16-bit to 24-bit leaves unchanged.

    Once a client has reported decoded audio, a server summary without a source
    hash is a violation rather than missing evidence, because the server
    adapters belong to this harness and always know the clip they were given.
    """
    codec_label = expected_codec.upper()
    if expected_codec not in _LOSSLESS_CODECS:
        return None, f"{codec_label} is lossy, so its decoded audio cannot equal the source"

    server_audio = server_summary.get("audio", {})
    client_audio = client_summary.get("audio", {})
    received_hash = client_audio.get("received_pcm_sha256")
    if (
        not isinstance(received_hash, str)
        or not received_hash
        or client_audio.get("received_sample_count") == 0
    ):
        return None, "the client reported no decoded audio"

    source_hash = server_audio.get("source_pcm_sha256")
    if not isinstance(source_hash, str) or not source_hash:
        return "Server summary is missing the source PCM hash", None

    stream = server_summary.get("stream") or {}
    converted = [
        f"{field} {server_audio.get(field)} -> {stream.get(field)}"
        for field in ("sample_rate", "channels")
        if stream.get(field) != server_audio.get(field)
    ]
    if converted:
        return None, (
            "the stream was not negotiated at the source clip's format "
            f"({', '.join(converted)})"
        )

    if received_hash == source_hash:
        return None, None
    return (
        f"{codec_label} decoded audio does not match the source clip: "
        f"{_sample_count_detail(server_audio, client_audio)}"
        f"source={source_hash} client={received_hash}"
    ), None


def _microseconds(value: Any) -> int | None:
    """Return a microsecond summary field as an int, or None when it is unusable."""
    # bool is an int subclass, and a summary field can hold any JSON value.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _first_metadata_state_verdict(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
) -> tuple[bool, str | None]:
    """Judge the first metadata-carrying `server/state`, as (evaluated, violation).

    `violation` is why the state breaks the spec, or None when it does not.
    `evaluated` reports whether both halves of the evidence were present to
    judge at all, so a caller can distinguish a rule that held from one that
    never ran.

    The first `server/state` sent for a role on a connection MUST carry a past
    or present `timestamp` if the role object has one, so the client is brought
    up to date before any scheduled update follows. A future timestamp is a
    scheduled update, which leaves a freshly connected client holding state it
    must not display yet and nothing to show in the meantime.

    Both numbers are read in the server's own clock domain: the stamp the
    client received verbatim, against a clock reading the server adapter took
    once that same state had been stamped. So the comparison is exact and needs
    neither clock synchronization nor a tolerance. Judging it from the client's
    time filter instead is not an option, because that filter needs two samples
    to converge and the first metadata state arrives before the second lands.

    The bound is read once the frame carrying that state is on the wire, not
    when the send was queued. A reading taken before transmission would call a
    timestamp future when it was already past by the time it went out, and so
    blame a conformant server; a later reading can only be more permissive.

    Both sides describe the first metadata-carrying state independently, so the
    two are required to name the same timestamp before the bound is applied. If
    they differ, the client was looking at a state the server did not record and
    there is no bound that belongs to it, so nothing is judged.

    None means there is nothing to report, which deliberately covers both no
    violation and no evidence this harness could read, as
    `declared_formats.undeclared_format_violation` does: an adapter that does
    not record its side yet is not thereby non-conformant. Only `aiosendspin`
    records both sides today. A client that reports the observation without a
    `timestamp_us` key is read the same way, so an adapter can publish which
    observation was first without being taken to claim anything about its
    timestamp.

    A `timestamp_us` of None is a violation rather than missing evidence,
    because the adapter reports having seen the object and found no timestamp on
    it. That branch is unreachable through `aiosendspin`, whose model makes
    `timestamp` required and so drops an object without one while parsing; such
    a case fails its zero-update check first.

    The spec puts this requirement on any role object carrying a `timestamp`,
    which is `color` as well as `metadata`. There is no `color` scenario in the
    matrix, so this reads the metadata summary directly rather than generalizing
    over roles for a single caller.
    """
    first_state = client_summary.get("metadata", {}).get("first_object_state")
    if not isinstance(first_state, dict):
        return False, None
    if "timestamp_us" not in first_state:
        return False, None

    if first_state["timestamp_us"] is None:
        return True, (
            "The first server/state carrying a metadata object had no timestamp; "
            "the spec requires one so the client is current before any scheduled update"
        )

    stamped_us = _microseconds(first_state["timestamp_us"])
    if stamped_us is None:
        return False, None

    sent = server_summary.get("metadata", {}).get("first_state_sent")
    if not isinstance(sent, dict):
        return False, None
    sent_us = _microseconds(sent.get("timestamp_us"))
    bound_us = _microseconds(sent.get("bound_us"))
    if sent_us is None or bound_us is None:
        return False, None
    if stamped_us != sent_us:
        return False, None

    if stamped_us > bound_us:
        return True, (
            "The first server/state carrying a metadata object was scheduled "
            f"{(stamped_us - bound_us) / 1_000:.1f} ms ahead of the server clock "
            f"(timestamp={stamped_us}, server clock once sent={bound_us}); "
            "the spec requires a past or present timestamp"
        )
    return True, None


def _compare_metadata_summaries(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
) -> tuple[bool, str]:
    if server_summary.get("status") != "ok":
        return False, f"Server summary status is {server_summary.get('status')!r}"
    if client_summary.get("status") != "ok":
        return False, f"Client summary status is {client_summary.get('status')!r}"

    expected = server_summary.get("metadata", {}).get("expected")
    received = client_summary.get("metadata", {}).get("received")
    update_count = int(client_summary.get("metadata", {}).get("update_count") or 0)
    if update_count <= 0:
        return False, "Client summary shows zero metadata updates"
    if expected != received:
        return False, f"Metadata mismatch: server={expected!r} client={received!r}"
    checked, violation = _first_metadata_state_verdict(server_summary, client_summary)
    if violation is not None:
        return False, violation
    if checked:
        return True, (
            "Metadata snapshot matches and the first state carried a past or present timestamp"
        )
    return True, "Metadata snapshot matches"


def _compare_controller_summaries(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
) -> tuple[bool, str]:
    if server_summary.get("status") != "ok":
        return False, f"Server summary status is {server_summary.get('status')!r}"
    if client_summary.get("status") != "ok":
        return False, f"Client summary status is {client_summary.get('status')!r}"

    server_controller = server_summary.get("controller", {})
    expected = server_controller.get("expected_command")
    received = server_controller.get("received_command")
    sent = client_summary.get("controller", {}).get("sent_command")
    if received is None:
        return False, "Server summary shows no controller command received"
    if sent is None:
        return False, "Client summary shows no controller command sent"
    if not (expected == received == sent):
        return (
            False,
            "Controller command mismatch: "
            f"expected={expected!r} server={received!r} client={sent!r}",
        )

    # volume/muted/repeat/shuffle are controller-role state; the client must observe
    # and report them, and RC1 makes all four mandatory. repeat/shuffle must never
    # appear in the metadata scenario.
    received_state = client_summary.get("controller", {}).get("received_state")
    if not isinstance(received_state, dict):
        return False, "Client summary shows no controller state"
    for field in ("volume", "muted", "repeat", "shuffle"):
        if field not in received_state:
            return False, f"Client controller state is missing {field}"
        expected_value = server_controller.get(field)
        received_value = received_state.get(field)
        if expected_value != received_value:
            return (
                False,
                f"Controller {field} mismatch: "
                f"server={expected_value!r} client={received_value!r}",
            )

    ok, reason = _compare_controller_supported_commands(server_controller, received_state)
    if not ok:
        return False, reason

    # RC1: a command MUST be one of the values listed in supported_commands from the
    # latest controller state the client received. received_state is the client's
    # final state, so this holds only while a scenario advertises one stable set;
    # a scenario that mutates the set mid-session needs a send-time snapshot instead.
    command_name = expected.get("command") if isinstance(expected, dict) else None
    if command_name not in received_state["supported_commands"]:
        return (
            False,
            f"Controller command {command_name!r} is absent from the "
            "supported_commands the client observed",
        )
    return True, f"Controller command matched ({command_name})"


def _compare_controller_supported_commands(
    server_controller: dict[str, Any],
    received_state: dict[str, Any],
) -> tuple[bool, str]:
    """
    Require the client to have observed exactly the command set the server advertised.

    RC1 makes `supported_commands` a free subset of the command vocabulary, so the
    only thing to assert is that both ends agree on it; which commands a server
    chooses to offer is its own business.
    """
    advertised = server_controller.get("supported_commands")
    if not isinstance(advertised, list):
        return False, "Server summary shows no advertised supported_commands"
    if "supported_commands" not in received_state:
        return False, "Client controller state is missing supported_commands"
    observed = received_state.get("supported_commands")
    if not isinstance(observed, list):
        return False, "Client controller state reports no supported_commands list"

    unobserved = sorted(set(advertised) - set(observed))
    unadvertised = sorted(set(observed) - set(advertised))
    if not unobserved and not unadvertised:
        return True, "Controller supported_commands match"

    details = []
    if unobserved:
        details.append(f"advertised but not observed: {unobserved}")
    if unadvertised:
        details.append(f"observed but not advertised: {unadvertised}")
    return False, "Controller supported_commands mismatch: " + "; ".join(details)


def _compare_artwork_summaries(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
) -> tuple[bool, str]:
    if server_summary.get("status") != "ok":
        return False, f"Server summary status is {server_summary.get('status')!r}"
    if client_summary.get("status") != "ok":
        return False, f"Client summary status is {client_summary.get('status')!r}"

    expected = server_summary.get("artwork", {})
    received = client_summary.get("artwork", {})
    if int(received.get("received_count") or 0) <= 0:
        return False, "Client summary shows zero artwork frames"
    if (
        expected.get("channel") == received.get("channel")
        and expected.get("encoded_sha256") == received.get("received_sha256")
    ):
        return True, "Artwork bytes match"
    return (
        False,
        "Artwork mismatch: "
        f"server={expected.get('encoded_sha256')} client={received.get('received_sha256')}",
    )


def _format_label(fmt: dict[str, Any] | None) -> str:
    if not fmt:
        return "none"
    return (
        f"{fmt.get('codec')}/{fmt.get('sample_rate')}Hz/"
        f"{fmt.get('bit_depth')}bit/{fmt.get('channels')}ch"
    )


def _format_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Match on every field the requester constrained; ``None`` means unconstrained."""
    for key in AUDIO_FORMAT_FIELDS:
        wanted = expected.get(key)
        if wanted is not None and actual.get(key) != wanted:
            return False
    return True


def _formats_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in AUDIO_FORMAT_FIELDS)


def _compare_format_preference_summaries(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
) -> tuple[bool, str]:
    if server_summary.get("status") != "ok":
        return False, f"Server summary status is {server_summary.get('status')!r}"
    if client_summary.get("status") != "ok":
        return False, f"Client summary status is {client_summary.get('status')!r}"

    # The preference has to be observed on the server side of the wire. A client
    # reporting that it asked for a format does not show which message carried it.
    preference = server_summary.get("format_preference")
    received = preference.get("received") if isinstance(preference, dict) else None
    if not isinstance(received, dict):
        return (
            False,
            "Server received no player format preference in client/state "
            "(roles/player/v1.md, client/state player object `format`)",
        )

    renegotiation = client_summary.get("renegotiation")
    if not isinstance(renegotiation, dict):
        return False, "Client summary missing renegotiation block"

    requested = renegotiation.get("requested")
    if not isinstance(requested, dict):
        return False, "Client did not record a requested format"

    if not _format_matches(received, requested):
        return (
            False,
            f"Server received client/state format {_format_label(received)} but the "
            f"client reports preferring {_format_label(requested)}",
        )

    stream_start_count = int(renegotiation.get("stream_start_count") or 0)
    final = renegotiation.get("final_format")
    if stream_start_count < 2 or not isinstance(final, dict):
        return (
            False,
            "Server did not send a new stream/start after the client/state format "
            f"changed (stream_start_count={stream_start_count}; roles/player/v1.md, "
            "Format preference)",
        )

    if not _format_matches(final, requested):
        return (
            False,
            f"Renegotiated format {_format_label(final)} does not match "
            f"requested {_format_label(requested)}",
        )

    initial = renegotiation.get("initial_format")
    if isinstance(initial, dict) and _formats_equal(initial, final):
        return False, f"Format did not change (stayed {_format_label(final)})"

    return True, f"Renegotiated {_format_label(initial)} -> {_format_label(final)}"


def _compare_summaries(
    scenario: ScenarioSpec,
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
) -> tuple[bool, str]:
    reported = _adapter_reported_failures({"server": server_summary, "client": client_summary})
    if reported is not None:
        return False, reported
    matches, reason = _dispatch_comparison(scenario, server_summary, client_summary)
    if not matches:
        return matches, reason
    # Every format the case negotiated must be one the client declared. Modes
    # that negotiate no player stream leave the check with nothing to inspect,
    # so it applies to all of them rather than to a list of audio modes that a
    # later scenario could be forgotten from.
    violation = undeclared_format_violation(server_summary, client_summary)
    if violation is not None:
        return False, violation
    return matches, reason


def _dispatch_comparison(
    scenario: ScenarioSpec,
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
) -> tuple[bool, str]:
    if scenario.verification_mode == "protocol":
        if server_summary.get("status") != "ok":
            return False, f"Server summary status is {server_summary.get('status')!r}"
        if client_summary.get("status") != "ok":
            return False, f"Client summary status is {client_summary.get('status')!r}"
        failure = protocol_evidence_failure(
            server_summary,
            client_summary,
            assertion_ids=scenario.protocol_assertions,
        )
        if failure is not None:
            return False, failure
        return True, f"Passed {len(scenario.protocol_assertions)} protocol assertions"
    if scenario.verification_mode == "audio-pcm":
        return _compare_audio_summaries(server_summary, client_summary)
    if scenario.verification_mode == "audio-encoded-bytes":
        return _compare_encoded_audio_summaries(
            server_summary,
            client_summary,
            expected_codec=scenario.preferred_codec,
        )
    if scenario.verification_mode == "metadata":
        return _compare_metadata_summaries(server_summary, client_summary)
    if scenario.verification_mode == "controller":
        return _compare_controller_summaries(server_summary, client_summary)
    if scenario.verification_mode == "artwork":
        return _compare_artwork_summaries(server_summary, client_summary)
    if scenario.verification_mode == "format-preference":
        return _compare_format_preference_summaries(server_summary, client_summary)
    raise ValueError(f"Unsupported verification mode: {scenario.verification_mode}")


async def _run_failfast_role(
    *,
    context: CaseContext,
    failing_role: RoleName,
    failure_reason: str | None = None,
) -> CaseResult:
    failing_impl = context.implementation(failing_role)
    summary_path = context.summary_path(failing_role)
    ready_path = context.ready_path(failing_role)
    log_path = context.log_path(failing_role)
    registry_path = context.registry_path
    extra_args = context.role_args(failing_role)

    if failure_reason is not None:
        cmd = _python_adapter_command(
            "conformance.adapters.placeholder",
            summary=str(summary_path),
            ready=str(ready_path),
            registry=str(registry_path),
            implementation=failing_impl,
            role=failing_role,
            failure_reason=failure_reason,
            **extra_args,
        )
    else:
        cmd = _build_role_command(
            failing_impl,
            failing_role,
            summary=summary_path,
            ready=ready_path,
            registry=registry_path,
            extra_args=extra_args,
        )
    if cmd is None:
        return _case_result(
            context,
            status="failed",
            reason=(
                "Required runtime toolchain is not available for "
                f"{failing_impl} {failing_role} adapter"
            ),
        )

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    dotnet_repo = resolve_repo_path("sendspin-dotnet")
    if dotnet_repo is not None:
        env["SendspinDotnetRepo"] = str(dotnet_repo)
    process = await collect_process(
        cmd.cmd,
        cwd=cmd.cwd,
        env=env,
        log_path=log_path,
    )
    try:
        await wait_for_file(ready_path, timeout_s=10)
        await wait_for_exit(process, role=failing_role, timeout_s=5)
    except Exception as err:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if process.returncode is None:
            await process.wait()
        return _case_result(
            context,
            status="failed",
            reason=str(err),
            server_exit_code=process.returncode if failing_role == "server" else None,
            client_exit_code=process.returncode if failing_role == "client" else None,
        )
    finally:
        await close_process_log(process)

    reason = f"{failing_impl} {failing_role} adapter failed without writing a summary"
    if summary_path.exists():
        payload = read_json(summary_path)
        reason = str(payload.get("reason") or reason)

    return _case_result(
        context,
        status="failed",
        reason=reason,
        server_exit_code=process.returncode if failing_role == "server" else None,
        client_exit_code=process.returncode if failing_role == "client" else None,
    )


async def run_case(
    *,
    results_dir: Path,
    scenario_id: str,
    server_impl: str,
    client_impl: str,
    timeout_s: float,
    slot_index: int,
    build_index: dict[str, dict[str, Any]] | None = None,
    environment_id: str | None = None,
    environment_name: str | None = None,
) -> CaseResult:
    scenario = require_scenario(scenario_id)
    environment = resolve_environment(
        environment_id=environment_id,
        environment_name=environment_name,
    )
    context = CaseContext(
        results_dir=results_dir,
        environment_id=environment.id,
        environment_name=environment.name,
        scenario=scenario,
        server_impl=server_impl,
        client_impl=client_impl,
        timeout_s=timeout_s,
        slot_index=slot_index,
    )
    if context.case_dir.exists():
        shutil.rmtree(context.case_dir)
    context.case_dir.mkdir(parents=True, exist_ok=True)

    if not context.role_spec("server").supported:
        return _write_result(
            context,
            await _run_failfast_role(
                context=context,
                failing_role="server",
            ),
        )
    if not context.role_spec("client").supported:
        return _write_result(
            context,
            await _run_failfast_role(
                context=context,
                failing_role="client",
            ),
        )
    server_capability_reason = context.capability_failure("server")
    if server_capability_reason is not None:
        return _write_result(
            context,
            await _run_failfast_role(
                context=context,
                failing_role="server",
                failure_reason=server_capability_reason,
            ),
        )
    client_capability_reason = context.capability_failure("client")
    if client_capability_reason is not None:
        return _write_result(
            context,
            await _run_failfast_role(
                context=context,
                failing_role="client",
                failure_reason=client_capability_reason,
            ),
        )

    build_index = build_index or {}
    server_build_result = _role_build_result(context, "server", build_index=build_index)
    client_build_result = _role_build_result(context, "client", build_index=build_index)

    if server_build_result is not None and str(server_build_result.get("status")) != "built":
        return _write_result(
            context,
            await _run_failfast_role(
                context=context,
                failing_role="server",
                failure_reason=_build_failure_reason(server_build_result),
            ),
        )
    if client_build_result is not None and str(client_build_result.get("status")) != "built":
        return _write_result(
            context,
            await _run_failfast_role(
                context=context,
                failing_role="client",
                failure_reason=_build_failure_reason(client_build_result),
            ),
        )

    server_launch = _build_role_command(
        context.server_impl,
        "server",
        summary=context.summary_path("server"),
        ready=context.ready_path("server"),
        registry=context.registry_path,
        extra_args=context.role_args("server"),
        build_result=server_build_result,
    )
    client_launch = _build_role_command(
        context.client_impl,
        "client",
        summary=context.summary_path("client"),
        ready=context.ready_path("client"),
        registry=context.registry_path,
        extra_args=context.role_args("client"),
        build_result=client_build_result,
    )
    if server_launch is None or client_launch is None:
        reason = "Required runtime toolchain is not available for this adapter"
        if server_launch is None and server_build_result is not None:
            reason = (
                f"{context.server_impl} server adapter was built but no runnable artifact "
                "was available for the test matrix"
            )
        elif client_launch is None and client_build_result is not None:
            reason = (
                f"{context.client_impl} client adapter was built but no runnable artifact "
                "was available for the test matrix"
            )
        return _write_result(
            context,
            _case_result(
                context,
                status="failed",
                reason=reason,
            ),
        )

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"

    server_process = await collect_process(
        server_launch.cmd,
        cwd=server_launch.cwd,
        env=env,
        log_path=context.log_path("server"),
    )
    client_process: asyncio.subprocess.Process | None = None
    try:
        await wait_for_file(context.ready_path("server"), timeout_s=10)
        client_process = await collect_process(
            client_launch.cmd,
            cwd=client_launch.cwd,
            env=env,
            log_path=context.log_path("client"),
        )
        await wait_for_file(context.ready_path("client"), timeout_s=10)

        await wait_for_exit(server_process, role="server", timeout_s=timeout_s)
        await wait_for_exit(client_process, role="client", timeout_s=10)
    except Exception as err:
        if server_process.returncode is None:
            try:
                server_process.kill()
            except ProcessLookupError:
                pass
        if client_process is not None and client_process.returncode is None:
            try:
                client_process.kill()
            except ProcessLookupError:
                pass
        if server_process.returncode is None:
            await server_process.wait()
        if client_process is not None and client_process.returncode is None:
            await client_process.wait()
        return _write_result(
            context,
            _case_result(
                context,
                status="failed",
                reason=_incomplete_case_reason(context, str(err) or err.__class__.__name__),
                server_exit_code=server_process.returncode,
                client_exit_code=None if client_process is None else client_process.returncode,
            ),
        )
    finally:
        await close_process_log(server_process)
        if client_process is not None:
            await close_process_log(client_process)

    if not context.summary_path("server").exists() or not context.summary_path("client").exists():
        return _write_result(
            context,
            _case_result(
                context,
                status="failed",
                reason=_incomplete_case_reason(
                    context,
                    _missing_summary_reason(
                        context=context,
                        server_process=server_process,
                        client_process=client_process,
                    ),
                ),
                server_exit_code=server_process.returncode,
                client_exit_code=None if client_process is None else client_process.returncode,
            ),
        )

    server_payload = read_json(context.summary_path("server"))
    client_payload = read_json(context.summary_path("client"))
    try:
        matches, comparison_reason = _compare_summaries(
            scenario,
            server_payload,
            client_payload,
        )
    except Exception as err:  # noqa: BLE001 - never let one bad case crash the whole matrix run
        matches = False
        comparison_reason = f"Comparison raised {err.__class__.__name__}: {err}"
    if not matches:
        status = "failed"
        reason = comparison_reason
    elif server_process.returncode != 0 or client_process is None or client_process.returncode != 0:
        status = "failed"
        reason = "One or more adapters exited non-zero"
    else:
        status = "passed"
        reason = comparison_reason

    return _write_result(
        context,
        _case_result(
            context,
            status=status,
            reason=reason,
            server_exit_code=server_process.returncode,
            client_exit_code=None if client_process is None else client_process.returncode,
        ),
    )


async def run_matrix(
    *,
    results_dir: Path,
    from_filter: str | None,
    to_filter: str | None,
    timeout_s: float,
    jobs: int = 1,
    build_results: list[dict[str, Any]] | None = None,
    environment_id: str | None = None,
    environment_name: str | None = None,
) -> list[dict[str, Any]]:
    """Run the current scenario matrix with optional filters."""
    if jobs <= 0:
        raise ValueError(f"jobs must be positive, got {jobs}")
    data_dir = results_dir / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    if results_dir.exists():
        for child in results_dir.iterdir():
            if child.name == "data":
                continue
            if child.is_dir() and (child / "result.json").exists():
                shutil.rmtree(child)
        for filename in ("index.json", "results.json"):
            legacy_file = results_dir / filename
            if legacy_file.exists():
                legacy_file.unlink()
    data_dir.mkdir(parents=True, exist_ok=True)
    build_index = _build_result_index(build_results)
    server_impls = parse_implementation_filter(from_filter)
    client_impls = parse_implementation_filter(to_filter)
    cases: list[tuple[int, str, str, str]] = []
    slot_index = 0
    for scenario in ordered_scenarios():
        scenario_server_impls = implementations_for_scenario(
            role="server",
            scenario=scenario,
            names=server_impls,
        )
        for server_impl in scenario_server_impls:
            for client_impl in client_impls:
                cases.append((slot_index, scenario.id, server_impl, client_impl))
                slot_index += 1

    semaphore = asyncio.Semaphore(jobs)
    resource_locks: dict[str, asyncio.Lock] = {}

    async def _run_limited(
        *,
        slot_index: int,
        scenario_id: str,
        server_impl: str,
        client_impl: str,
    ) -> dict[str, Any]:
        async with semaphore:
            async with AsyncExitStack() as stack:
                for resource_key in _case_resource_keys(
                    scenario_id=scenario_id,
                    server_impl=server_impl,
                    client_impl=client_impl,
                ):
                    await stack.enter_async_context(
                        resource_locks.setdefault(resource_key, asyncio.Lock())
                    )
                result = await run_case(
                    results_dir=data_dir,
                    scenario_id=scenario_id,
                    server_impl=server_impl,
                    client_impl=client_impl,
                    timeout_s=timeout_s,
                    slot_index=slot_index,
                    build_index=build_index,
                    environment_id=environment_id,
                    environment_name=environment_name,
                )
                return result.__dict__

    results = await asyncio.gather(
        *[
            _run_limited(
                slot_index=slot_index,
                scenario_id=scenario_id,
                server_impl=server_impl,
                client_impl=client_impl,
            )
            for slot_index, scenario_id, server_impl, client_impl in cases
        ]
    )
    write_json(data_dir / "index.json", {"results": results})
    return results
