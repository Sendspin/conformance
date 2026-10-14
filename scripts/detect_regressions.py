#!/usr/bin/env python3
"""Detect test regressions by comparing local results against the published baseline.

Fetches the current index.json from the live GitHub Pages site and compares it
against a local results directory.  A *regression* is any case that was
``passed`` in the baseline but is no longer ``passed`` in the local run.

A cell is exempt when its baseline row was produced by a different version of the
scenario: the baseline row's ``scenario_revision`` differs from the current row's.
A baseline published before the field existed carries none at all and exempts
every cell, until the first publish that stamps it.  Every other cell keeps full
protection.

Exit codes:
    0 – no regressions detected (or baseline unavailable)
    1 – one or more regressions found

Optional outputs:
    --discord-file PATH   write a Discord-ready notification to PATH
    --github-summary      append a Markdown summary to $GITHUB_STEP_SUMMARY
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from conformance.io import read_json

BUMP_HINT = (
    "If a scenario changed deliberately, bump its `scenario_revision` in "
    "src/conformance/scenarios.py to reset its baseline."
)

BASELINE_URL = (
    "https://sendspin.github.io/conformance/data/index.json"
)


def _case_key(result: dict[str, object]) -> tuple[str, str, str]:
    """Unique identity for a matrix cell, ignoring environment."""
    return (
        str(result["scenario_id"]),
        str(result["server_impl"]),
        str(result["client_impl"]),
    )


def fetch_baseline(url: str, timeout: int = 30) -> list[dict[str, object]] | None:
    """Fetch the published baseline index.json, returning None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sendspin-conformance-ci"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
            return list(payload["results"])
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, KeyError) as exc:
        print(f"Warning: could not fetch baseline from {url}: {exc}", file=sys.stderr)
        return None


def _baseline_passed_index(
    baseline: list[dict[str, object]],
) -> dict[tuple[str, str, str], dict[str, object]]:
    """Index the baseline rows that passed, by matrix cell."""
    return {_case_key(result): result for result in baseline if result["status"] == "passed"}


def _baseline_stamps_revisions(baseline: list[dict[str, object]]) -> bool:
    """Report whether the published baseline carries scenario revisions at all."""
    return any("scenario_revision" in result for result in baseline)


def require_scenario_revisions(current: list[dict[str, object]]) -> None:
    """Raise when the local run did not stamp a scenario revision on every result row."""
    missing = sorted(
        {
            str(result["scenario_id"])
            for result in current
            if not isinstance(result.get("scenario_revision"), int)
        }
    )
    if missing:
        raise ValueError(
            "Local results carry no integer scenario_revision for: "
            + ", ".join(missing)
            + ". Every scenario must declare one; see ScenarioSpec.scenario_revision."
        )


def exemption_reason(
    baseline_result: dict[str, object],
    current_result: dict[str, object],
    *,
    baseline_is_stamped: bool,
) -> str | None:
    """
    Explain why a baseline row is not comparable to the current one, if it is not.

    Returns None when both rows were produced by the same version of the scenario
    and the cell therefore keeps full regression protection.

    `baseline_is_stamped` says whether the published baseline carries the field at
    all.  A baseline published before it existed cannot be compared to anything, so
    every cell is exempt for that one release; once the baseline does carry the
    field, a row missing it is a broken contract rather than an old publish, and
    the cell keeps its protection instead of quietly losing it.
    """
    if "scenario_revision" not in baseline_result:
        return None if baseline_is_stamped else "baseline predates scenario revision stamping"
    if baseline_result["scenario_revision"] != current_result["scenario_revision"]:
        return "scenario revision changed since the baseline"
    return None


def detect_regressions(
    baseline: list[dict[str, object]],
    current: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return current results that regressed from a comparable passed baseline."""
    baseline_passed = _baseline_passed_index(baseline)
    baseline_is_stamped = _baseline_stamps_revisions(baseline)

    regressions: list[dict[str, object]] = []
    for result in current:
        baseline_result = baseline_passed.get(_case_key(result))
        if baseline_result is None:
            continue
        reason = exemption_reason(
            baseline_result, result, baseline_is_stamped=baseline_is_stamped
        )
        if reason is not None:
            continue
        if result["status"] != "passed":
            regressions.append(result)
    return regressions


def exempt_cases(
    baseline: list[dict[str, object]],
    current: list[dict[str, object]],
) -> list[tuple[dict[str, object], str]]:
    """
    Return the results a regression would have been reported for, with the reason.

    A cell that is still passing suppressed nothing, so it is left out — the list
    exists to show what the exemption actually cost in protection.
    """
    baseline_passed = _baseline_passed_index(baseline)
    baseline_is_stamped = _baseline_stamps_revisions(baseline)

    exempt: list[tuple[dict[str, object], str]] = []
    for result in current:
        baseline_result = baseline_passed.get(_case_key(result))
        if baseline_result is None or result["status"] == "passed":
            continue
        reason = exemption_reason(
            baseline_result, result, baseline_is_stamped=baseline_is_stamped
        )
        if reason is not None:
            exempt.append((result, reason))
    return exempt


def format_exemption_summary(exempt: list[tuple[dict[str, object], str]]) -> str:
    """Format exempt cells as one line per reason, naming the scenarios involved."""
    by_reason: dict[str, list[str]] = {}
    for result, reason in exempt:
        by_reason.setdefault(reason, []).append(str(result["scenario_id"]))

    return "\n".join(
        f"  {len(scenarios)} cell(s) exempt — {reason}: "
        f"{', '.join(sorted(set(scenarios)))}"
        for reason, scenarios in sorted(by_reason.items())
    )


def format_regression_table(regressions: list[dict[str, object]]) -> str:
    """Format regressions as a human-readable table."""
    lines: list[str] = []
    for r in sorted(regressions, key=_case_key):
        lines.append(
            f"  {r['scenario_id']}  {r['server_impl']} -> {r['client_impl']}  "
            f"[{r['status']}]"
        )
    return "\n".join(lines)


def format_github_summary(regressions: list[dict[str, object]]) -> str:
    """Format regressions as Markdown for $GITHUB_STEP_SUMMARY."""
    lines: list[str] = [
        "## Conformance regressions detected",
        "",
        f"**{len(regressions)}** test(s) that previously passed are now failing.",
        "",
        BUMP_HINT,
        "",
        "| Scenario | Server | Client | Status |",
        "| --- | --- | --- | --- |",
    ]
    for r in sorted(regressions, key=_case_key):
        lines.append(
            f"| {r['scenario_id']} | {r['server_impl']} | {r['client_impl']} "
            f"| {r['status']} |"
        )
    return "\n".join(lines)


def format_discord_message(regressions: list[dict[str, object]]) -> str:
    """Build a Discord notification for regressions found during site publish."""
    count = len(regressions)
    header = (
        f"\u26a0\ufe0f **Conformance regression{'s' if count != 1 else ''} detected** "
        f"— {count} test{'s' if count != 1 else ''} that previously passed "
        f"{'are' if count != 1 else 'is'} now failing."
    )

    rows: list[str] = []
    for r in sorted(regressions, key=_case_key):
        rows.append(
            f"- **{r['scenario_id']}**: {r['server_impl']} \u2192 {r['client_impl']} "
            f"[{r['status']}]"
        )

    body = "\n".join(rows)

    footer = textwrap.dedent("""\
        Review the full report: <https://sendspin.github.io/conformance/>
    """).strip()

    return f"{header}\n\n{body}\n\n{footer}\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        default=str(ROOT / "artifacts" / "results"),
        help="Path to the local results directory (default: artifacts/results)",
    )
    parser.add_argument(
        "--baseline-url",
        default=BASELINE_URL,
        help="URL of the published baseline index.json",
    )
    parser.add_argument(
        "--discord-file",
        help="Write Discord notification text to this file when regressions are found",
    )
    parser.add_argument(
        "--github-summary",
        action="store_true",
        help="Append regression summary to $GITHUB_STEP_SUMMARY",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    results_dir = Path(args.results_dir)
    index_path = results_dir / "data" / "index.json"
    if not index_path.exists():
        print(f"Error: local results not found at {index_path}", file=sys.stderr)
        return 1

    current_results: list[dict[str, object]] = list(read_json(index_path)["results"])
    try:
        require_scenario_revisions(current_results)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Local results: {len(current_results)} cases", flush=True)

    baseline = fetch_baseline(args.baseline_url)
    if baseline is None:
        print("Baseline unavailable — skipping regression check.", flush=True)
        return 0

    passed_in_baseline = sum(1 for r in baseline if r["status"] == "passed")
    print(
        f"Baseline: {len(baseline)} cases ({passed_in_baseline} passed)",
        flush=True,
    )

    exempt = exempt_cases(baseline, current_results)
    if exempt:
        print(
            "Exempt from comparison (would otherwise be reported as regressions):\n"
            f"{format_exemption_summary(exempt)}",
            flush=True,
        )

    regressions = detect_regressions(baseline, current_results)

    if not regressions:
        print("No regressions detected.", flush=True)
        return 0

    print(
        f"\n{len(regressions)} regression(s) detected:\n"
        f"{format_regression_table(regressions)}\n"
        f"{BUMP_HINT}\n",
        flush=True,
    )

    if args.github_summary:
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write("\n" + format_github_summary(regressions) + "\n")

    if args.discord_file:
        discord_path = Path(args.discord_file)
        discord_path.parent.mkdir(parents=True, exist_ok=True)
        discord_path.write_text(format_discord_message(regressions), encoding="utf-8")
        print(f"Discord notification written to {discord_path}", flush=True)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
