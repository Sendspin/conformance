"""Coverage for how a `CONFORMANCE_REPO_*` override selects a checkout.

An override says "use this checkout". One that names a path which does not
exist must stop the run, because resolving to `repos/<name>` instead would
audit a different implementation than the operator asked for without saying so.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conformance import build, cli
from conformance.implementations import (
    resolve_repo_path,
    resolve_required_repo_path,
    validate_repo_overrides,
)
from conformance.paths import RepoOverrideError, candidate_repo_paths


class RepoOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.managed = self.root / "conformance" / "repos" / "spec"
        self.managed.mkdir(parents=True)
        self.elsewhere = self.root / "elsewhere" / "spec"
        self.elsewhere.mkdir(parents=True)
        self.missing = self.root / "missing" / "spec"

        cleared = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("CONFORMANCE_REPO_")
        }
        for patcher in (
            mock.patch.dict(os.environ, cleared, clear=True),
            mock.patch("conformance.paths.repo_root", return_value=self.root / "conformance"),
            mock.patch("conformance.paths.workspace_root", return_value=self.root),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_no_override_resolves_the_default_checkout(self) -> None:
        self.assertEqual(candidate_repo_paths("spec"), [self.managed, self.root / "spec"])
        self.assertEqual(resolve_repo_path("spec"), self.managed)
        validate_repo_overrides()

    def test_no_override_and_no_checkout_resolves_to_none(self) -> None:
        self.assertIsNone(resolve_repo_path("sendspin-cli"))
        with self.assertRaises(FileNotFoundError):
            resolve_required_repo_path("sendspin-cli")

    def test_an_empty_override_counts_as_unset(self) -> None:
        os.environ["CONFORMANCE_REPO_SPEC"] = ""
        self.assertEqual(resolve_repo_path("spec"), self.managed)
        validate_repo_overrides()

    def test_an_existing_override_is_used_over_the_default_checkout(self) -> None:
        os.environ["CONFORMANCE_REPO_SPEC"] = str(self.elsewhere)
        self.assertEqual(resolve_repo_path("spec"), self.elsewhere)
        validate_repo_overrides()

    def test_a_nonexistent_override_fails_instead_of_resolving_elsewhere(self) -> None:
        os.environ["CONFORMANCE_REPO_SPEC"] = str(self.missing)
        for resolve in (resolve_repo_path, resolve_required_repo_path):
            with self.subTest(resolve=resolve.__name__):
                with self.assertRaises(RepoOverrideError) as raised:
                    resolve("spec")
                self.assertIn("CONFORMANCE_REPO_SPEC", str(raised.exception))
                self.assertIn(str(self.missing), str(raised.exception))

    def test_a_relative_override_fails_even_when_it_exists(self) -> None:
        relative = os.path.relpath(self.elsewhere)
        self.assertTrue(Path(relative).exists())
        os.environ["CONFORMANCE_REPO_SPEC"] = relative
        with self.assertRaises(RepoOverrideError) as raised:
            resolve_repo_path("spec")
        self.assertIn("CONFORMANCE_REPO_SPEC", str(raised.exception))
        self.assertIn(relative, str(raised.exception))

    def test_every_nonexistent_override_is_named_at_once(self) -> None:
        os.environ["CONFORMANCE_REPO_SPEC"] = str(self.missing)
        os.environ["CONFORMANCE_REPO_SENDSPIN_RS"] = str(self.root / "missing" / "rs")
        os.environ["CONFORMANCE_REPO_SENDSPIN_CLI"] = str(self.elsewhere)
        with self.assertRaises(RepoOverrideError) as raised:
            validate_repo_overrides()
        message = str(raised.exception)
        self.assertIn("CONFORMANCE_REPO_SPEC", message)
        self.assertIn("CONFORMANCE_REPO_SENDSPIN_RS", message)
        self.assertNotIn("CONFORMANCE_REPO_SENDSPIN_CLI", message)

    def test_a_build_step_does_not_report_it_as_a_failed_build(self) -> None:
        os.environ["CONFORMANCE_REPO_SENDSPIN_JVM"] = str(self.root / "missing" / "jvm")
        with self.assertRaises(RepoOverrideError):
            build._gradle_build_result()

    def test_the_cli_stops_before_building_or_running(self) -> None:
        os.environ["CONFORMANCE_REPO_SPEC"] = str(self.missing)
        for command in ("build", "run"):
            with (
                self.subTest(command=command),
                mock.patch("sys.argv", ["conformance", command]),
                mock.patch.object(cli, "build_adapters") as build_adapters,
                mock.patch.object(cli, "run_matrix") as run_matrix,
                mock.patch("sys.stderr"),
                self.assertRaises(SystemExit) as raised,
            ):
                cli.main()
            self.assertEqual(raised.exception.code, 2)
            build_adapters.assert_not_called()
            run_matrix.assert_not_called()


if __name__ == "__main__":
    unittest.main()
