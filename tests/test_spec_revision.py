"""Coverage for recording the spec revision a report was audited against.

The published run always has a `repos/spec` checkout, so the interesting rules
here — that the spec leads the repository list, and that a missing checkout
degrades instead of failing the run — are never exercised by the matrix.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from conformance.paths import env_repo_override_key
from conformance.repository_versions import collect_repository_versions
from conformance.site import _spec_revision_chip

SPEC_OVERRIDE = env_repo_override_key("spec")
MISSING_CHECKOUT = Path("/nonexistent/spec")


@contextmanager
def _spec_checkout(path: Path) -> Iterator[None]:
    """Point spec resolution at one path, restoring any real override afterwards."""
    previous = os.environ.get(SPEC_OVERRIDE)
    os.environ[SPEC_OVERRIDE] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(SPEC_OVERRIDE, None)
        else:
            os.environ[SPEC_OVERRIDE] = previous


def _git(repo: Path, *args: str) -> None:
    """Run one git command, isolated from whatever the contributor configured globally."""
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def _init_repo(repo: Path, *, tag: str | None = None) -> None:
    """Create a single-commit repository, optionally sitting on one tag."""
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "spec@example.invalid")
    _git(repo, "config", "user.name", "Spec")
    (repo / "spec.md").write_text("# Sendspin\n", encoding="utf-8")
    _git(repo, "add", "spec.md")
    _git(repo, "commit", "-qm", "Define the protocol")
    if tag is not None:
        _git(repo, "tag", tag)


def _spec_entry() -> dict[str, Any]:
    """Return the spec entry, which leads the repository list."""
    return collect_repository_versions([])[0]


class SpecRepositoryEntryTest(unittest.TestCase):
    def test_spec_leads_the_repository_list(self) -> None:
        results = [{"server_impl": "aiosendspin", "client_impl": "aiosendspin"}]
        with _spec_checkout(MISSING_CHECKOUT):
            repositories = collect_repository_versions(results)
        self.assertEqual(repositories[0]["key"], "spec")
        self.assertIn("aiosendspin", [entry["key"] for entry in repositories[1:]])

    def test_tagged_checkout_records_the_tag_not_only_the_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, tag="1.0.0-rc1")
            with _spec_checkout(repo):
                entry = _spec_entry()
        self.assertTrue(entry["available"])
        self.assertEqual(entry["revision_label"], "1.0.0-rc1")
        self.assertTrue(entry["commit_sha"])
        self.assertEqual(entry["remote_url"], "https://github.com/Sendspin/spec")

    def test_untagged_checkout_falls_back_to_the_short_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            with _spec_checkout(repo):
                entry = _spec_entry()
        self.assertEqual(entry["revision_label"], entry["commit_short_sha"])

    def test_missing_checkout_degrades_instead_of_failing(self) -> None:
        with _spec_checkout(MISSING_CHECKOUT):
            entry = _spec_entry()
        self.assertEqual(entry["key"], "spec")
        self.assertFalse(entry["available"])
        self.assertTrue(entry["reason"])


class SpecRevisionChipTest(unittest.TestCase):
    def test_links_the_label_to_the_commit(self) -> None:
        chip = _spec_revision_chip(
            [
                {
                    "key": "spec",
                    "available": True,
                    "revision_label": "1.0.0-rc1",
                    "commit_url": "https://github.com/Sendspin/spec/commit/671a34d",
                }
            ]
        )
        self.assertIn("Spec 1.0.0-rc1", chip)
        self.assertIn("https://github.com/Sendspin/spec/commit/671a34d", chip)

    def test_renders_nothing_when_the_checkout_was_unavailable(self) -> None:
        chip = _spec_revision_chip(
            [
                {
                    "key": "spec",
                    "available": False,
                    "reason": "Repository checkout was not available.",
                }
            ]
        )
        self.assertEqual(chip, "")

    def test_renders_nothing_when_no_spec_entry_is_present(self) -> None:
        self.assertEqual(_spec_revision_chip([{"key": "aiosendspin", "available": True}]), "")

    def test_one_host_without_a_checkout_does_not_hide_the_revision(self) -> None:
        """A merged report keeps one spec entry per host, unavailable ones included."""
        chip = _spec_revision_chip(
            [
                {"key": "spec", "available": False, "reason": "Checkout was not available."},
                {
                    "key": "spec",
                    "available": True,
                    "revision_label": "1.0.0-rc1",
                    "commit_url": "https://github.com/Sendspin/spec/commit/671a34d",
                },
            ]
        )
        self.assertIn("Spec 1.0.0-rc1", chip)

    def test_hosts_on_different_revisions_claim_no_single_revision(self) -> None:
        """Naming one of two revisions would assert a reading half the matrix contradicts."""
        chip = _spec_revision_chip(
            [
                {"key": "spec", "available": True, "revision_label": "1.0.0-rc1"},
                {"key": "spec", "available": True, "revision_label": "1.0.0-rc2"},
            ]
        )
        self.assertEqual(chip, "")


if __name__ == "__main__":
    unittest.main()
