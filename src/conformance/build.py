"""Adapter build helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from .environment import build_log_filename, resolve_environment
from .implementations import ensure_repo_checkout, resolve_repo_path
from .io import write_json
from .paths import repo_root
from .toolchains import find_cargo, find_cmake, find_dotnet, find_go, find_swift


BuildResult = dict[str, Any]
BuildPlan = tuple[tuple[str, Callable[[], BuildResult]], ...]


def _run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd or repo_root()),
        env=env,
        capture_output=True,
        text=True,
    )


def _trim_output(stdout: str, stderr: str, *, limit: int = 4000) -> str:
    combined = (stdout + "\n" + stderr).strip()
    if len(combined) <= limit:
        return combined
    return combined[-limit:]


def _timed_result(builder: Callable[[], BuildResult]) -> BuildResult:
    start = time.perf_counter()
    result = builder()
    result["duration_seconds"] = round(time.perf_counter() - start, 3)
    return result


def _cargo_binary_path(manifest: Path) -> Path:
    with manifest.open("rb") as handle:
        package = tomllib.load(handle)["package"]
    suffix = ".exe" if os.name == "nt" else ""
    return manifest.parent / "target" / "debug" / f"{package['name']}{suffix}"


def _dotnet_dll_path(project: Path) -> Path:
    root = ET.parse(project).getroot()
    target_framework = project.stem
    assembly_name = project.stem
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        text = (node.text or "").strip()
        if not text:
            continue
        if tag == "TargetFramework":
            target_framework = text
        elif tag == "AssemblyName":
            assembly_name = text
    return project.parent / "bin" / "Debug" / target_framework / f"{assembly_name}.dll"


def _swift_bin_path(swift: str, package_dir: Path) -> Path | None:
    completed = _run_command([swift, "build", "--package-path", str(package_dir), "--show-bin-path"])
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    if not output:
        return None
    return Path(output)


def _built_result(
    *,
    adapter: str,
    completed: subprocess.CompletedProcess[str],
    runtime_command_prefix: list[str] | None = None,
) -> BuildResult:
    result: BuildResult = {
        "adapter": adapter,
        "status": "built" if completed.returncode == 0 else "failed",
        "detail": _trim_output(completed.stdout, completed.stderr),
    }
    if runtime_command_prefix is not None:
        result["runtime_command_prefix"] = runtime_command_prefix
    return result


def _python_build_result() -> BuildResult:
    completed = _run_command([sys.executable, "-m", "compileall", "src", "scripts"])
    return _built_result(adapter="python-adapters", completed=completed)


def _dotnet_build_result() -> BuildResult:
    dotnet = find_dotnet()
    dotnet_repo = resolve_repo_path("sendspin-dotnet")
    dotnet_project = (
        repo_root()
        / "adapters"
        / "sendspin-dotnet"
        / "client"
        / "Conformance.SendspinDotnet.Client.csproj"
    )
    if dotnet is None:
        return {
            "adapter": "sendspin-dotnet-client",
            "status": "failed",
            "detail": "dotnet executable is not available",
        }
    if dotnet_repo is None:
        return {
            "adapter": "sendspin-dotnet-client",
            "status": "failed",
            "detail": "sendspin-dotnet repository checkout was not found",
        }

    env = dict(os.environ)
    env["SendspinDotnetRepo"] = str(dotnet_repo)
    completed = _run_command([dotnet, "build", str(dotnet_project)], env=env)
    runtime_command_prefix = None
    if completed.returncode == 0:
        dll_path = _dotnet_dll_path(dotnet_project)
        if dll_path.exists():
            runtime_command_prefix = [dotnet, str(dll_path)]
    return _built_result(
        adapter="sendspin-dotnet-client",
        completed=completed,
        runtime_command_prefix=runtime_command_prefix,
    )


def _node_build_result() -> BuildResult:
    node = shutil.which("node")
    if node is None:
        return {
            "adapter": "sendspin-js-adapters",
            "status": "failed",
            "detail": "node executable is not available",
        }
    sendspin_js_repo = resolve_repo_path("sendspin-js")
    if sendspin_js_repo is None:
        return {
            "adapter": "sendspin-js-adapters",
            "status": "failed",
            "detail": "sendspin-js repository checkout was not found",
        }

    npm = shutil.which("npm")
    if npm is None:
        return {
            "adapter": "sendspin-js-adapters",
            "status": "failed",
            "detail": "npm executable is not available",
        }

    install = _run_command([npm, "install"], cwd=sendspin_js_repo)
    if install.returncode != 0:
        return {
            "adapter": "sendspin-js-adapters",
            "status": "failed",
            "detail": _trim_output(install.stdout, install.stderr),
        }

    build = _run_command([npm, "run", "build"], cwd=sendspin_js_repo)
    if build.returncode != 0:
        return {
            "adapter": "sendspin-js-adapters",
            "status": "failed",
            "detail": _trim_output(build.stdout, build.stderr),
        }

    adapter_dir = repo_root() / "adapters" / "sendspin-js"
    adapter_install = _run_command(
        [npm, "install", "--package-lock=false"],
        cwd=adapter_dir,
    )
    if adapter_install.returncode != 0:
        return {
            "adapter": "sendspin-js-adapters",
            "status": "failed",
            "detail": _trim_output(adapter_install.stdout, adapter_install.stderr),
        }

    scripts = [
        repo_root() / "adapters" / "sendspin-js" / "client.mjs",
    ]
    completed = _run_command([node, "--check", *[str(script) for script in scripts]])
    return _built_result(adapter="sendspin-js-adapters", completed=completed)


def _cargo_build_result() -> BuildResult:
    cargo = find_cargo()
    if cargo is None:
        return {
            "adapter": "sendspin-rs-client",
            "status": "failed",
            "detail": "cargo executable is not available",
        }

    try:
        ensure_repo_checkout("sendspin-rs")
    except FileNotFoundError as err:
        return {
            "adapter": "sendspin-rs-client",
            "status": "failed",
            "detail": str(err),
        }

    manifest = repo_root() / "adapters" / "sendspin-rs" / "client" / "Cargo.toml"
    completed = _run_command([cargo, "build", "--manifest-path", str(manifest)])
    runtime_command_prefix = None
    if completed.returncode == 0:
        binary_path = _cargo_binary_path(manifest)
        if binary_path.exists():
            runtime_command_prefix = [str(binary_path)]
    return _built_result(
        adapter="sendspin-rs-client",
        completed=completed,
        runtime_command_prefix=runtime_command_prefix,
    )


def _swift_build_result() -> BuildResult:
    swift = find_swift()
    if swift is None:
        return {
            "adapter": "SendspinKit-client",
            "status": "failed",
            "detail": "swift executable is not available",
        }
    if sys.platform != "darwin":
        return {
            "adapter": "SendspinKit-client",
            "status": "failed",
            "detail": "SendspinKit client build currently requires macOS",
        }

    try:
        ensure_repo_checkout("SendspinKit")
    except FileNotFoundError as err:
        return {
            "adapter": "SendspinKit-client",
            "status": "failed",
            "detail": str(err),
        }

    package_dir = repo_root() / "adapters" / "SendspinKit" / "client"
    completed = _run_command([swift, "build", "--package-path", str(package_dir)])
    runtime_command_prefix = None
    if completed.returncode == 0:
        bin_path = _swift_bin_path(swift, package_dir)
        executable = None if bin_path is None else bin_path / "ConformanceSendspinKitClient"
        if executable is not None and executable.exists():
            runtime_command_prefix = [str(executable)]
    return _built_result(
        adapter="SendspinKit-client",
        completed=completed,
        runtime_command_prefix=runtime_command_prefix,
    )


def _cmake_build_result() -> BuildResult:
    cmake = find_cmake()
    if cmake is None:
        return {
            "adapter": "sendspin-cpp-client",
            "status": "failed",
            "detail": "cmake executable is not available",
        }

    try:
        ensure_repo_checkout("sendspin-cpp")
    except FileNotFoundError as err:
        return {
            "adapter": "sendspin-cpp-client",
            "status": "failed",
            "detail": str(err),
        }

    adapter_root = repo_root() / "adapters" / "sendspin-cpp" / "client"
    build_dir = adapter_root / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    binary_path = build_dir / f"conformance-sendspin-cpp-client{suffix}"

    configure = _run_command(
        [cmake, "-S", str(adapter_root), "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release"],
    )
    if configure.returncode != 0:
        return _built_result(adapter="sendspin-cpp-client", completed=configure)

    completed = _run_command(
        [cmake, "--build", str(build_dir), "--config", "Release"],
    )
    runtime_command_prefix = None
    if completed.returncode == 0 and binary_path.exists():
        runtime_command_prefix = [str(binary_path)]
    return _built_result(
        adapter="sendspin-cpp-client",
        completed=completed,
        runtime_command_prefix=runtime_command_prefix,
    )


def _go_build_result(
    *,
    adapter: str,
    package: str,
    output_name: str,
) -> BuildResult:
    go = find_go()
    if go is None:
        return {
            "adapter": adapter,
            "status": "failed",
            "detail": "go executable is not available",
        }

    try:
        ensure_repo_checkout("sendspin-go")
    except FileNotFoundError as err:
        return {
            "adapter": adapter,
            "status": "failed",
            "detail": str(err),
        }

    adapter_root = repo_root() / "adapters" / "sendspin-go"
    bin_dir = adapter_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    binary_path = bin_dir / f"{output_name}{suffix}"
    completed = _run_command(
        [go, "build", "-o", str(binary_path), f"./{package}"],
        cwd=adapter_root,
    )
    runtime_command_prefix = None
    if completed.returncode == 0 and binary_path.exists():
        runtime_command_prefix = [str(binary_path)]
    return _built_result(
        adapter=adapter,
        completed=completed,
        runtime_command_prefix=runtime_command_prefix,
    )


def build_adapters(report_path: Path | None = None) -> list[BuildResult]:
    """Build adapter sources when the required toolchains are available."""
    return build_selected_adapters(report_path=report_path)


def _build_plan() -> BuildPlan:
    return (
        ("python-adapters", _python_build_result),
        ("sendspin-dotnet-client", _dotnet_build_result),
        ("sendspin-rs-client", _cargo_build_result),
        ("SendspinKit-client", _swift_build_result),
        ("sendspin-cpp-client", _cmake_build_result),
        (
            "sendspin-go-client",
            lambda: _go_build_result(
                adapter="sendspin-go-client",
                package="client",
                output_name="sendspin-go-client",
            ),
        ),
        (
            "sendspin-go-server",
            lambda: _go_build_result(
                adapter="sendspin-go-server",
                package="server",
                output_name="sendspin-go-server",
            ),
        ),
        ("sendspin-js-adapters", _node_build_result),
    )


def build_selected_adapters(
    *,
    selected_adapters: set[str] | None = None,
    report_path: Path | None = None,
) -> list[BuildResult]:
    """Build only the requested adapters, or every adapter when no selection is provided."""
    results = [
        _timed_result(builder)
        for adapter, builder in _build_plan()
        if selected_adapters is None or adapter in selected_adapters
    ]
    if report_path is not None:
        write_json(report_path, {"results": results})
    return results


def annotate_build_results(
    results: list[BuildResult],
    *,
    environment_id: str | None = None,
    environment_name: str | None = None,
) -> list[BuildResult]:
    """Attach stable environment metadata to build results."""
    environment = resolve_environment(
        environment_id=environment_id,
        environment_name=environment_name,
    )
    return [
        {
            **result,
            "environment_id": environment.id,
            "environment_name": environment.name,
        }
        for result in results
    ]


def write_build_artifacts(
    data_dir: Path,
    results: list[BuildResult],
    *,
    environment_id: str | None = None,
    environment_name: str | None = None,
) -> list[BuildResult]:
    """Persist build metadata and per-adapter build logs into the results data directory."""
    annotated = annotate_build_results(
        results,
        environment_id=environment_id,
        environment_name=environment_name,
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    write_json(data_dir / "build-report.json", {"results": annotated})

    builds_dir = data_dir / "builds"
    builds_dir.mkdir(parents=True, exist_ok=True)

    for result in annotated:
        adapter = str(result["adapter"])
        environment = str(result["environment_id"])
        detail = str(result.get("detail") or "").strip() or "No build detail recorded."
        (builds_dir / build_log_filename(environment, adapter)).write_text(
            detail + "\n",
            encoding="utf-8",
        )
    return annotated


def build_failed(results: list[BuildResult]) -> bool:
    """Return True when any adapter build failed."""
    return any(result["status"] == "failed" for result in results)
