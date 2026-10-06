"""Repository revision metadata for report publishing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .implementations import IMPLEMENTATIONS, SUPPORTING_REPOS, resolve_repo_path
from .io import write_json
from .paths import repo_root


def _run_git(repo: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    return output or None


def _normalize_remote_url(remote_url: str | None) -> str | None:
    if remote_url is None:
        return None
    value = remote_url.strip()
    if not value:
        return None
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value[len("git@github.com:") :]
    if value.endswith(".git"):
        value = value[:-4]
    return value


def _commit_urls(remote_url: str | None, commit_sha: str | None) -> tuple[str | None, str | None]:
    normalized = _normalize_remote_url(remote_url)
    if normalized is None:
        return None, None
    repo_url = normalized
    if commit_sha is None:
        return repo_url, None
    return repo_url, f"{repo_url}/commit/{quote(commit_sha, safe='')}"


def _blob_url(remote_url: str | None, commit_sha: str | None, repo_path: str | None) -> str | None:
    normalized = _normalize_remote_url(remote_url)
    if normalized is None or commit_sha is None or repo_path is None:
        return None
    return (
        f"{normalized}/blob/{quote(commit_sha, safe='')}/"
        f"{quote(repo_path, safe='/')}"
    )


def _preferred_remote_url(key: str, remote_url: str | None) -> str | None:
    specification = IMPLEMENTATIONS.get(key)
    if specification is not None:
        return f"https://github.com/Sendspin/{quote(specification.repo_dirname, safe='')}"
    return _normalize_remote_url(remote_url)


def _conformance_client_repo_path(key: str) -> str | None:
    return {
        "aiosendspin": "src/conformance/adapters/aiosendspin_client.py",
        "sendspin-dotnet": "adapters/sendspin-dotnet/client/Program.cs",
        "SendspinKit": "adapters/SendspinKit/client/Sources/ConformanceSendspinKitClient/main.swift",
        "sendspin-js": "adapters/sendspin-js/client.mjs",
        "sendspin-rs": "adapters/sendspin-rs/client/src/main.rs",
        "sendspin-cpp": "adapters/sendspin-cpp/client/main.cpp",
        "sendspin-go": "adapters/sendspin-go/client/main.go",
    }.get(key)


def _head_details(repo: Path) -> tuple[str | None, str | None, str | None, str | None]:
    raw = _run_git(repo, "log", "-1", "--format=%H%x00%h%x00%s%x00%cs")
    if raw is None:
        return None, None, None, None
    commit_sha, short_sha, subject, committed_at = raw.split("\x00", 3)
    return commit_sha, short_sha, subject, committed_at


def _revision_label(repo: Path) -> str | None:
    """
    Return a human-readable label for the checked-out revision.

    The tag when HEAD sits on one (`1.0.0-rc1`), the nearest tag plus a distance
    when it does not (`1.0.0-rc1-5-gabc1234`), otherwise the short sha. A
    checkout with uncommitted changes gains a `-dirty` suffix, so the label is
    not always a resolvable ref.
    """
    return _run_git(repo, "describe", "--tags", "--always", "--dirty")


def _latest_tag(repo: Path) -> str | None:
    return _run_git(
        repo,
        "for-each-ref",
        "--sort=-creatordate",
        "--count=1",
        "--format=%(refname:short)",
        "refs/tags",
    )


def _ahead_of_tag(repo: Path, tag: str) -> int | None:
    raw = _run_git(repo, "rev-list", "--count", f"{tag}..HEAD")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _used_implementation_names(results: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    for result in results:
        server_impl = result.get("server_impl")
        client_impl = result.get("client_impl")
        if isinstance(server_impl, str):
            seen.add(server_impl)
        if isinstance(client_impl, str):
            seen.add(client_impl)

    ordered = [name for name in IMPLEMENTATIONS if name in seen]
    ordered.extend(sorted(name for name in seen if name not in IMPLEMENTATIONS))
    return ordered


def _environment_entries(
    *,
    environment_id: str | None,
    environment_name: str | None,
) -> list[dict[str, str]]:
    if not environment_id and not environment_name:
        return []
    return [
        {
            "id": environment_id or "default",
            "name": environment_name or environment_id or "default",
        }
    ]


def _repository_entry(
    *,
    key: str,
    display_name: str,
    repo_path: Path | None,
    remote_url: str | None,
    environments: list[dict[str, str]],
) -> dict[str, Any]:
    preferred_remote_url = _preferred_remote_url(key, remote_url)
    entry: dict[str, Any] = {
        "key": key,
        "display_name": display_name,
        "repo_path": str(repo_path) if repo_path is not None else None,
        "remote_url": preferred_remote_url,
        "environments": environments,
        "available": False,
    }
    if repo_path is None or not repo_path.exists():
        entry["reason"] = "Repository checkout was not available while building this report."
        return entry

    origin_url = _preferred_remote_url(
        key,
        _run_git(repo_path, "remote", "get-url", "origin") or remote_url,
    )
    commit_sha, short_sha, subject, committed_at = _head_details(repo_path)
    revision_label = _revision_label(repo_path)
    latest_tag = _latest_tag(repo_path)
    commits_ahead = None if latest_tag is None else _ahead_of_tag(repo_path, latest_tag)
    repo_url, commit_url = _commit_urls(origin_url, commit_sha)

    entry.update(
        {
            "available": commit_sha is not None,
            "remote_url": repo_url,
            "commit_sha": commit_sha,
            "commit_short_sha": short_sha,
            "commit_subject": subject,
            "committed_at": committed_at,
            "commit_url": commit_url,
            "revision_label": revision_label,
            "latest_release_tag": latest_tag,
            "commits_ahead_of_release": commits_ahead,
            "release_url": (
                None
                if repo_url is None or latest_tag is None
                else f"{repo_url}/releases/tag/{quote(latest_tag, safe='')}"
            ),
            "compare_url": (
                None
                if repo_url is None or latest_tag is None or commit_sha is None
                else f"{repo_url}/compare/{quote(latest_tag, safe='')}...{quote(commit_sha, safe='')}"
            ),
        }
    )
    if commit_sha is None:
        entry["reason"] = "Git metadata was unavailable for this checkout."
    return entry


def collect_repository_versions(
    results: list[dict[str, Any]],
    *,
    environment_id: str | None = None,
    environment_name: str | None = None,
) -> list[dict[str, Any]]:
    """Collect revision metadata for the spec and the implementations a report covers."""
    environments = _environment_entries(
        environment_id=environment_id,
        environment_name=environment_name,
    )
    conformance_repo = repo_root()
    conformance_remote_url = (
        _run_git(conformance_repo, "remote", "get-url", "origin")
        or "https://github.com/Sendspin/conformance.git"
    )
    conformance_commit_sha, _, _, _ = _head_details(conformance_repo)
    spec_dirname, spec_remote_url = SUPPORTING_REPOS["spec"]
    repositories: list[dict[str, Any]] = [
        _repository_entry(
            key="spec",
            display_name="Sendspin spec",
            repo_path=resolve_repo_path(spec_dirname),
            remote_url=spec_remote_url,
            environments=environments,
        )
    ]
    for implementation_name in _used_implementation_names(results):
        specification = IMPLEMENTATIONS.get(implementation_name)
        if specification is None:
            continue
        entry = _repository_entry(
            key=implementation_name,
            display_name=specification.display_name,
            repo_path=resolve_repo_path(specification.repo_dirname),
            remote_url=specification.remote_url,
            environments=environments,
        )
        client_code_path = _conformance_client_repo_path(implementation_name)
        client_code_url = _blob_url(
            conformance_remote_url,
            conformance_commit_sha,
            client_code_path,
        )
        if client_code_url is not None:
            entry["client_code_url"] = client_code_url
        repositories.append(entry)
    return repositories


def write_repository_versions(
    data_dir: Path,
    results: list[dict[str, Any]],
    *,
    environment_id: str | None = None,
    environment_name: str | None = None,
) -> list[dict[str, Any]]:
    """Persist repository revision metadata into one results directory."""
    repositories = collect_repository_versions(
        results,
        environment_id=environment_id,
        environment_name=environment_name,
    )
    write_json(data_dir / "repositories.json", {"repositories": repositories})
    return repositories
