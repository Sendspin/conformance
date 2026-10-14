"""Coverage for which repository the report links each implementation to.

The report links the repository the registry names, not whatever a checkout's
`origin` points at, so a fork or a `CONFORMANCE_REPO_*` override cannot redirect
a row. Implementations live in more than one GitHub organization, so the link
comes from each entry's `remote_url` rather than from a fixed organization.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from conformance.implementations import IMPLEMENTATIONS
from conformance.repository_versions import collect_repository_versions


def _entry(key: str, checkout: Path | None) -> dict[str, Any]:
    """Return the repository entry the report records for one implementation."""
    results = [{"server_impl": key, "client_impl": key}]
    repositories = collect_repository_versions(results, resolve_repo=lambda dirname: checkout)
    return next(entry for entry in repositories if entry["key"] == key)


class RepositoryLinkTest(unittest.TestCase):
    def test_every_implementation_links_its_registered_repository(self) -> None:
        for key, specification in IMPLEMENTATIONS.items():
            with self.subTest(implementation=key):
                self.assertEqual(
                    _entry(key, None)["remote_url"],
                    specification.remote_url.removesuffix(".git"),
                )

    def test_sendspin_organization_links_are_unchanged(self) -> None:
        for key, specification in IMPLEMENTATIONS.items():
            if not specification.remote_url.startswith("https://github.com/Sendspin/"):
                continue
            with self.subTest(implementation=key):
                self.assertEqual(
                    _entry(key, None)["remote_url"],
                    f"https://github.com/Sendspin/{specification.repo_dirname}",
                )

    def test_a_checkout_cloned_from_a_fork_still_links_the_registered_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for args in (
                ("init", "-q", "."),
                ("remote", "add", "origin", "git@github.com:someone/SendspinDroid.git"),
                ("-c", "user.email=a@example.invalid", "-c", "user.name=A",
                 "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "Start"),
            ):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            entry = _entry("android", repo)
        self.assertEqual(entry["remote_url"], "https://github.com/chrisuthe/SendspinDroid")
        self.assertTrue(
            entry["commit_url"].startswith("https://github.com/chrisuthe/SendspinDroid/commit/")
        )


if __name__ == "__main__":
    unittest.main()
