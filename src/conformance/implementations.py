"""Implementation registry and adapter metadata."""

from __future__ import annotations

from pathlib import Path

from .models import ImplementationSpec, RoleName, RoleSpec, ScenarioSpec
from .paths import candidate_repo_paths, first_existing_path, repo_root


IMPLEMENTATIONS: dict[str, ImplementationSpec] = {
    "sendspin-jvm": ImplementationSpec(
        name="sendspin-jvm",
        display_name="sendspin-jvm",
        repo_dirname="sendspin-jvm",
        remote_url="https://github.com/OnFreund/sendspin-jvm.git",
        client=RoleSpec(
            supported=True,
            adapter_kind="python",
            build_adapter="python-adapters",
            entrypoint="conformance.adapters.sendspin_jvm_client",
            supports_server_initiated=True,
            supports_client_initiated=True,
            supports_flac=True,
            supports_opus=False,
            supports_discovery=False,
            supported_role_families=("player", "metadata", "controller", "artwork"),
        ),
        server=RoleSpec(
            supported=False,
            adapter_kind="placeholder",
            entrypoint="conformance.adapters.placeholder",
            reason="sendspin-jvm is a client-only implementation.",
        ),
    ),
    "sendspin-android-tv": ImplementationSpec(
        name="sendspin-android-tv",
        display_name="sendspin-android-tv",
        repo_dirname="sendspin-android-tv",
        remote_url="https://github.com/OnFreund/sendspin-android-tv.git",
        client=RoleSpec(
            supported=True,
            adapter_kind="python",
            build_adapter="python-adapters",
            entrypoint="conformance.adapters.sendspin_android_tv_client",
            supports_server_initiated=True,
            supports_client_initiated=True,
            supports_flac=True,
            supports_opus=False,
            supports_discovery=False,
            supported_role_families=("player", "metadata", "controller", "artwork"),
        ),
        server=RoleSpec(
            supported=False,
            adapter_kind="placeholder",
            entrypoint="conformance.adapters.placeholder",
            reason="sendspin-android-tv is a client-only implementation.",
        ),
    ),
    "aiosendspin": ImplementationSpec(
        name="aiosendspin",
        display_name="aiosendspin",
        repo_dirname="aiosendspin",
        remote_url="https://github.com/Sendspin/aiosendspin.git",
        client=RoleSpec(
            supported=True,
            adapter_kind="python",
            build_adapter="python-adapters",
            entrypoint="conformance.adapters.aiosendspin_client",
            supports_server_initiated=True,
            supports_client_initiated=True,
            supports_flac=True,
            supports_opus=True,
            supports_discovery=True,
            supported_role_families=("player", "metadata", "controller", "artwork"),
        ),
        server=RoleSpec(
            supported=True,
            adapter_kind="python",
            build_adapter="python-adapters",
            entrypoint="conformance.adapters.aiosendspin_server",
            supports_server_initiated=True,
            supports_client_initiated=True,
            supports_flac=True,
            supports_opus=True,
            supports_discovery=True,
            supported_role_families=("player", "metadata", "controller", "artwork"),
        ),
    ),
    "sendspin-dotnet": ImplementationSpec(
        name="sendspin-dotnet",
        display_name="sendspin-dotnet",
        repo_dirname="sendspin-dotnet",
        remote_url="https://github.com/Sendspin/sendspin-dotnet.git",
        client=RoleSpec(
            supported=True,
            adapter_kind="dotnet",
            build_adapter="sendspin-dotnet-client",
            entrypoint="adapters/sendspin-dotnet/client/Conformance.SendspinDotnet.Client.csproj",
            supports_server_initiated=True,
            supports_client_initiated=True,
            supports_flac=True,
            supports_discovery=True,
            supported_role_families=("player", "metadata", "controller", "artwork"),
        ),
        server=RoleSpec(
            supported=False,
            adapter_kind="placeholder",
            entrypoint="conformance.adapters.placeholder",
            reason="The checked-in .NET SDK exposes client host mode, not a server implementation.",
        ),
    ),
    "SendspinKit": ImplementationSpec(
        name="SendspinKit",
        display_name="SendspinKit",
        repo_dirname="SendspinKit",
        remote_url="https://github.com/Sendspin/SendspinKit.git",
        client=RoleSpec(
            supported=True,
            adapter_kind="swift",
            build_adapter="SendspinKit-client",
            entrypoint="adapters/SendspinKit/client:ConformanceSendspinKitClient",
            supports_server_initiated=True,
            supports_client_initiated=True,
            supports_flac=True,
            supported_role_families=("player", "metadata", "controller", "artwork"),
        ),
        server=RoleSpec(
            supported=False,
            adapter_kind="placeholder",
            entrypoint="conformance.adapters.placeholder",
            reason="SendspinKit is currently a client library in this workspace.",
        ),
    ),
    "sendspin-js": ImplementationSpec(
        name="sendspin-js",
        display_name="sendspin-js",
        repo_dirname="sendspin-js",
        remote_url="https://github.com/Sendspin/sendspin-js.git",
        client=RoleSpec(
            supported=True,
            adapter_kind="node",
            build_adapter="sendspin-js-adapters",
            entrypoint="adapters/sendspin-js/client.mjs",
            supports_server_initiated=True,
            supports_client_initiated=True,
            supported_role_families=("player", "metadata", "controller"),
        ),
        server=RoleSpec(
            supported=False,
            adapter_kind="placeholder",
            entrypoint="conformance.adapters.placeholder",
            reason="sendspin-js is currently a client library in this workspace.",
        ),
    ),
    "sendspin-rs": ImplementationSpec(
        name="sendspin-rs",
        display_name="sendspin-rs",
        repo_dirname="sendspin-rs",
        remote_url="https://github.com/Sendspin/sendspin-rs.git",
        client=RoleSpec(
            supported=True,
            adapter_kind="cargo",
            build_adapter="sendspin-rs-client",
            entrypoint="adapters/sendspin-rs/client/Cargo.toml",
            supports_server_initiated=True,
            supports_client_initiated=True,
            supports_flac=True,
            supported_role_families=("player", "metadata", "controller", "artwork"),
        ),
        server=RoleSpec(
            supported=False,
            adapter_kind="placeholder",
            entrypoint="conformance.adapters.placeholder",
            reason="sendspin-rs does not yet expose a server implementation in this workspace.",
        ),
    ),
    "sendspin-cpp": ImplementationSpec(
        name="sendspin-cpp",
        display_name="sendspin-cpp",
        repo_dirname="sendspin-cpp",
        remote_url="https://github.com/Sendspin/sendspin-cpp.git",
        client=RoleSpec(
            supported=True,
            adapter_kind="cmake",
            build_adapter="sendspin-cpp-client",
            entrypoint="adapters/sendspin-cpp/client",
            supports_server_initiated=True,
            supports_client_initiated=True,
            supports_flac=True,
            supported_role_families=("player", "metadata", "controller", "artwork"),
        ),
        server=RoleSpec(
            supported=False,
            adapter_kind="placeholder",
            entrypoint="conformance.adapters.placeholder",
            reason="sendspin-cpp is currently a client library in this workspace.",
        ),
    ),
    "sendspin-go": ImplementationSpec(
        name="sendspin-go",
        display_name="sendspin-go",
        repo_dirname="sendspin-go",
        remote_url="https://github.com/Sendspin/sendspin-go.git",
        client=RoleSpec(
            supported=True,
            adapter_kind="go",
            build_adapter="sendspin-go-client",
            entrypoint="adapters/sendspin-go/client",
            supports_server_initiated=True,
            supports_client_initiated=True,
            supports_flac=True,
            supports_discovery=True,
            supported_role_families=("player", "metadata", "controller", "artwork"),
        ),
        server=RoleSpec(
            supported=True,
            adapter_kind="go",
            build_adapter="sendspin-go-server",
            entrypoint="adapters/sendspin-go/server",
            supports_server_initiated=True,
            supports_client_initiated=True,
            supports_flac=True,
            supports_discovery=True,
            supported_role_families=("player", "metadata", "controller", "artwork"),
        ),
    ),
}


SUPPORTING_REPOS: dict[str, tuple[str, str]] = {
    "sendspin-cli": ("sendspin-cli", "https://github.com/Sendspin/sendspin-cli.git"),
    "spec": ("spec", "https://github.com/Sendspin/spec.git"),
}


def implementation_names() -> list[str]:
    """Return implementation names in stable order."""
    return sorted(IMPLEMENTATIONS)


def parse_implementation_filter(raw: str | None) -> list[str]:
    """Parse a comma-delimited implementation filter into known implementation names."""
    names = implementation_names()
    if not raw:
        return names
    selected = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [name for name in selected if name not in names]
    if unknown:
        raise ValueError(
            "Unknown implementation filter(s): " + ", ".join(sorted(unknown))
        )
    return selected


def selected_build_adapters(*, from_filter: str | None, to_filter: str | None) -> set[str]:
    """Return the build adapters required for the selected server/client matrix slice."""
    selected: set[str] = set()
    for implementation in parse_implementation_filter(from_filter):
        role_spec = IMPLEMENTATIONS[implementation].server
        if role_spec.supported and role_spec.build_adapter is not None:
            selected.add(role_spec.build_adapter)
    for implementation in parse_implementation_filter(to_filter):
        role_spec = IMPLEMENTATIONS[implementation].client
        if role_spec.supported and role_spec.build_adapter is not None:
            selected.add(role_spec.build_adapter)
    return selected


def role_supports_scenario(
    implementation: str,
    *,
    role: RoleName,
    scenario: ScenarioSpec,
) -> bool:
    """Return whether one implementation role can participate in a scenario."""
    specification = IMPLEMENTATIONS.get(implementation)
    if specification is None:
        return False
    role_spec = specification.server if role == "server" else specification.client
    if not role_spec.supported:
        return False
    return role_spec.unsupported_reason(
        implementation=implementation,
        role=role,
        scenario=scenario,
    ) is None


def implementations_for_scenario(
    *,
    role: RoleName,
    scenario: ScenarioSpec,
    names: list[str] | None = None,
) -> list[str]:
    """Return implementations whose role can actually participate in a scenario."""
    candidates = names if names is not None else implementation_names()
    return [
        implementation
        for implementation in candidates
        if role_supports_scenario(
            implementation,
            role=role,
            scenario=scenario,
        )
    ]


def resolve_repo_path(dirname: str) -> Path | None:
    """Resolve a repository checkout by its directory name."""
    return first_existing_path(candidate_repo_paths(dirname))


def resolve_required_repo_path(dirname: str) -> Path:
    """Resolve a repository checkout or raise a descriptive error."""
    repo = resolve_repo_path(dirname)
    if repo is None:
        raise FileNotFoundError(
            f"Could not find repository {dirname!r}. "
            f"Checked: {', '.join(str(path) for path in candidate_repo_paths(dirname))}"
        )
    return repo


def ensure_repo_checkout(dirname: str) -> Path:
    """Ensure a stable checkout path under repos/ exists and return it."""
    checkout = resolve_required_repo_path(dirname)
    managed_path = (repo_root() / "repos" / dirname).resolve()
    if managed_path == checkout.resolve():
        return managed_path

    managed_path.parent.mkdir(parents=True, exist_ok=True)
    if managed_path.exists():
        return managed_path

    try:
        managed_path.symlink_to(checkout, target_is_directory=True)
        return managed_path
    except OSError:
        return checkout
