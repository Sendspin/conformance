"""Coverage for the per-scenario revision that exempts a cell from comparison.

`scripts/detect_regressions.py` fails a PR on any matrix cell that passed in the
published baseline and no longer passes.  Changing what a scenario does turns its
cells red by design, so a cell is exempt when the baseline row it would be
compared against was produced by a different revision of that scenario.

The real check runs against a live remote baseline, so the rule is exercised here
against synthetic rows shaped like the ones the runner really writes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from conformance.runner import _case_result
from conformance.scenarios import ordered_scenarios, require_scenario
from detect_regressions import (
    detect_regressions,
    exempt_cases,
    exemption_reason,
    format_exemption_summary,
    require_scenario_revisions,
)


def _row(
    *,
    status: str,
    scenario_id: str = "server-initiated-flac",
    server_impl: str = "aiosendspin",
    client_impl: str = "sendspin-jvm",
    scenario_revision: int | None = 1,
) -> dict[str, Any]:
    """Build one index.json result row, omitting the revision when it is None."""
    row: dict[str, Any] = {
        "scenario_id": scenario_id,
        "server_impl": server_impl,
        "client_impl": client_impl,
        "status": status,
    }
    if scenario_revision is not None:
        row["scenario_revision"] = scenario_revision
    return row


def _reason(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    baseline_is_stamped: bool = False,
) -> str | None:
    """Ask for an exemption reason against an unstamped baseline unless told otherwise."""
    return exemption_reason(baseline, current, baseline_is_stamped=baseline_is_stamped)


class ExemptionReasonTest(unittest.TestCase):
    """The rule itself, independent of how the cells are indexed."""

    def test_same_revision_is_not_exempt(self) -> None:
        self.assertIsNone(_reason(_row(status="passed"), _row(status="failed")))

    def test_changed_revision_is_exempt(self) -> None:
        reason = _reason(
            _row(status="passed", scenario_revision=1),
            _row(status="failed", scenario_revision=2),
        )
        self.assertEqual(reason, "scenario revision changed since the baseline")

    def test_absent_baseline_revision_is_exempt(self) -> None:
        """The published baseline carries no revision until the first publish after this."""
        reason = _reason(
            _row(status="passed", scenario_revision=None),
            _row(status="failed", scenario_revision=1),
        )
        self.assertEqual(reason, "baseline predates scenario revision stamping")

    def test_a_stamped_baseline_retires_the_migration_exemption(self) -> None:
        """Once the baseline stamps revisions, a row missing one is a broken contract."""
        self.assertIsNone(
            _reason(
                _row(status="passed", scenario_revision=None),
                _row(status="failed", scenario_revision=1),
                baseline_is_stamped=True,
            )
        )

    def test_an_unstamped_local_row_raises_rather_than_exempting(self) -> None:
        """A local row is written by this checkout, so a missing revision is a bug."""
        with self.assertRaises(KeyError):
            _reason(
                _row(status="passed", scenario_revision=1),
                _row(status="failed", scenario_revision=None),
            )

    def test_a_lower_local_revision_is_exempt_too(self) -> None:
        """The revisions are compared for equality, not ordering."""
        self.assertIsNotNone(
            _reason(
                _row(status="passed", scenario_revision=2),
                _row(status="failed", scenario_revision=1),
            )
        )


class DetectRegressionsTest(unittest.TestCase):
    """The rule as the CI check applies it."""

    def test_unchanged_revision_still_reports_a_regression(self) -> None:
        regressions = detect_regressions(
            [_row(status="passed")],
            [_row(status="failed")],
        )
        self.assertEqual(len(regressions), 1)
        self.assertEqual(regressions[0]["status"], "failed")

    def test_a_skip_is_a_regression_too(self) -> None:
        """Anything short of `passed` loses a pass, not just `failed`."""
        self.assertEqual(
            len(detect_regressions([_row(status="passed")], [_row(status="skipped")])),
            1,
        )

    def test_changed_revision_suppresses_the_regression(self) -> None:
        self.assertEqual(
            detect_regressions(
                [_row(status="passed", scenario_revision=1)],
                [_row(status="failed", scenario_revision=2)],
            ),
            [],
        )

    def test_absent_baseline_revision_suppresses_the_regression(self) -> None:
        self.assertEqual(
            detect_regressions(
                [_row(status="passed", scenario_revision=None)],
                [_row(status="failed", scenario_revision=1)],
            ),
            [],
        )

    def test_a_still_passing_cell_is_never_a_regression(self) -> None:
        self.assertEqual(
            detect_regressions([_row(status="passed")], [_row(status="passed")]), []
        )

    def test_a_cell_that_never_passed_is_not_a_regression(self) -> None:
        self.assertEqual(
            detect_regressions([_row(status="failed")], [_row(status="failed")]), []
        )

    def test_the_exemption_is_per_scenario(self) -> None:
        """A bump must not disarm the check for the scenarios it did not touch."""
        baseline = [
            _row(scenario_id="server-initiated-flac", status="passed", scenario_revision=1),
            _row(scenario_id="server-initiated-pcm", status="passed", scenario_revision=1),
        ]
        current = [
            _row(scenario_id="server-initiated-flac", status="failed", scenario_revision=2),
            _row(scenario_id="server-initiated-pcm", status="failed", scenario_revision=1),
        ]
        regressions = detect_regressions(baseline, current)
        self.assertEqual(
            [r["scenario_id"] for r in regressions], ["server-initiated-pcm"]
        )

    def test_a_stamped_baseline_still_protects_a_row_that_lost_its_revision(self) -> None:
        """A baseline that stamps revisions must not be disarmed by one unstamped row."""
        baseline = [
            _row(client_impl="sendspin-jvm", status="passed", scenario_revision=None),
            _row(client_impl="sendspin-rs", status="passed", scenario_revision=1),
        ]
        current = [
            _row(client_impl="sendspin-jvm", status="failed", scenario_revision=1),
            _row(client_impl="sendspin-rs", status="failed", scenario_revision=1),
        ]
        self.assertEqual(len(detect_regressions(baseline, current)), 2)
        self.assertEqual(exempt_cases(baseline, current), [])

    def test_the_exemption_is_per_cell_within_a_scenario(self) -> None:
        """Cells are keyed by implementation pair, so one pair cannot mask another."""
        baseline = [
            _row(client_impl="sendspin-jvm", status="passed", scenario_revision=1),
            _row(client_impl="sendspin-rs", status="passed", scenario_revision=1),
        ]
        current = [
            _row(client_impl="sendspin-jvm", status="failed", scenario_revision=2),
            _row(client_impl="sendspin-rs", status="failed", scenario_revision=1),
        ]
        regressions = detect_regressions(baseline, current)
        self.assertEqual([r["client_impl"] for r in regressions], ["sendspin-rs"])


class ExemptCasesTest(unittest.TestCase):
    """What the check reports, so an exemption is never silent."""

    def test_exempt_cells_are_listed_with_their_reason(self) -> None:
        exempt = exempt_cases(
            [_row(status="passed", scenario_revision=1)],
            [_row(status="failed", scenario_revision=2)],
        )
        self.assertEqual(len(exempt), 1)
        self.assertEqual(exempt[0][1], "scenario revision changed since the baseline")

    def test_a_comparable_cell_is_not_listed(self) -> None:
        self.assertEqual(
            exempt_cases([_row(status="passed")], [_row(status="failed")]), []
        )

    def test_a_still_passing_cell_is_not_listed(self) -> None:
        """An exempt cell that still passes suppressed nothing, so it is not reported."""
        self.assertEqual(
            exempt_cases(
                [_row(status="passed", scenario_revision=1)],
                [_row(status="passed", scenario_revision=2)],
            ),
            [],
        )

    def test_a_cell_with_no_passing_baseline_is_not_listed(self) -> None:
        """Nothing was exempted — the cell had no baseline pass to protect."""
        self.assertEqual(
            exempt_cases(
                [_row(status="failed", scenario_revision=1)],
                [_row(status="failed", scenario_revision=2)],
            ),
            [],
        )

    def test_the_summary_counts_cells_and_names_each_scenario(self) -> None:
        cells = [
            ("server-initiated-flac", "sendspin-jvm"),
            ("server-initiated-flac", "sendspin-rs"),
            ("server-initiated-pcm", "sendspin-jvm"),
        ]
        exempt = exempt_cases(
            [
                _row(scenario_id=scenario, client_impl=client, status="passed")
                for scenario, client in cells
            ],
            [
                _row(
                    scenario_id=scenario,
                    client_impl=client,
                    status="failed",
                    scenario_revision=2,
                )
                for scenario, client in cells
            ],
        )
        summary = format_exemption_summary(exempt)
        self.assertEqual(
            summary.strip(),
            "3 cell(s) exempt — scenario revision changed since the baseline: "
            "server-initiated-flac, server-initiated-pcm",
        )

    def test_the_migration_reason_is_reported_for_an_unstamped_baseline(self) -> None:
        exempt = exempt_cases(
            [_row(status="passed", scenario_revision=None)],
            [_row(status="failed", scenario_revision=1)],
        )
        self.assertIn(
            "baseline predates scenario revision stamping",
            format_exemption_summary(exempt),
        )


class RequireSpecRevisionsTest(unittest.TestCase):
    """The local contract, checked up front so the failure names the scenario."""

    def test_a_fully_stamped_run_is_accepted(self) -> None:
        require_scenario_revisions([_row(status="failed"), _row(status="passed")])

    def test_an_unstamped_row_is_rejected_by_scenario_name(self) -> None:
        with self.assertRaises(ValueError) as caught:
            require_scenario_revisions(
                [
                    _row(scenario_id="server-initiated-flac", status="failed"),
                    _row(
                        scenario_id="server-initiated-pcm",
                        status="failed",
                        scenario_revision=None,
                    ),
                ]
            )
        self.assertIn("server-initiated-pcm", str(caught.exception))
        self.assertNotIn("server-initiated-flac", str(caught.exception))


class ResultRowTest(unittest.TestCase):
    """The revision has to reach the row, or the check above has nothing to read."""

    def test_every_scenario_declares_a_revision(self) -> None:
        for scenario in ordered_scenarios():
            with self.subTest(scenario=scenario.id):
                self.assertIsInstance(scenario.scenario_revision, int)
                self.assertGreaterEqual(scenario.scenario_revision, 1)

    def test_a_case_result_carries_its_scenario_revision(self) -> None:
        scenario = require_scenario("server-initiated-flac")
        context = SimpleNamespace(
            environment_id="macos",
            environment_name="macOS",
            scenario=scenario,
            server_impl="aiosendspin",
            client_impl="sendspin-jvm",
            case_dir=Path("results/data/case"),
        )
        result = _case_result(context, status="failed", reason="synthetic")
        self.assertEqual(result.scenario_revision, scenario.scenario_revision)
        self.assertIn("scenario_revision", result.__dict__)


if __name__ == "__main__":
    unittest.main()
