"""Path resolution helpers for local implementation repositories."""

from __future__ import annotations

import os
from pathlib import Path


class RepoOverrideError(Exception):
    """Raised when a CONFORMANCE_REPO_* override does not name an existing checkout."""


def repo_root() -> Path:
    """Return the conformance repository root."""
    return Path(__file__).resolve().parents[2]


def workspace_root() -> Path:
    """Return the parent workspace that may already contain sibling repos."""
    return repo_root().parent


def env_repo_override_key(name: str) -> str:
    """Return the environment variable used to override a repo path."""
    safe = name.upper().replace("-", "_")
    return f"CONFORMANCE_REPO_{safe}"


def repo_override_path(dirname: str) -> Path | None:
    """
    Return the checkout a CONFORMANCE_REPO_* override selects for a repository.

    Returns None when no override is set. Raises RepoOverrideError when the
    override is not an absolute path or names a path that does not exist.
    """
    key = env_repo_override_key(dirname)
    env_value = os.environ.get(key)
    if not env_value:
        return None
    path = Path(env_value).expanduser()
    # Adapters run from the repository root, so a relative path would name a
    # different directory there than in the process that was started.
    if not path.is_absolute():
        raise RepoOverrideError(f"{key}={env_value} is not an absolute path")
    path = path.resolve()
    if not path.exists():
        raise RepoOverrideError(f"{key}={env_value} points at {path}, which does not exist")
    return path


def candidate_repo_paths(dirname: str) -> list[Path]:
    """
    Return candidate locations for a repository checkout.

    A CONFORMANCE_REPO_* override is the only candidate when it is set. Raises
    RepoOverrideError when it is not an absolute path to something that exists.
    """
    override = repo_override_path(dirname)
    if override is not None:
        return [override]
    return [
        (repo_root() / "repos" / dirname).resolve(),
        (workspace_root() / dirname).resolve(),
    ]


def first_existing_path(paths: list[Path]) -> Path | None:
    """Return the first existing path from a candidate list."""
    for candidate in paths:
        if candidate.exists():
            return candidate
    return None
