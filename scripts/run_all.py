#!/usr/bin/env python3
"""Build adapters, run the matrix, and generate the static report."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from conformance.build import annotate_build_results, build_selected_adapters, write_build_artifacts
from conformance.environment import resolve_environment
from conformance.io import write_json
from conformance.implementations import selected_build_adapters, validate_repo_overrides
from conformance.paths import RepoOverrideError
from conformance.repository_versions import write_repository_versions
from conformance.runner import run_matrix
from conformance.scenarios import ordered_scenarios, require_scenario
from conformance.site import build_site


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", default=str(ROOT / "results"))
    parser.add_argument("--site-dir")
    parser.add_argument("--build-report-path", default=str(ROOT / "artifacts" / "build-report.json"))
    parser.add_argument("--from", dest="from_filter")
    parser.add_argument("--to", dest="to_filter")
    parser.add_argument("--timeout-seconds", type=float, default=40.0)
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--environment-id")
    parser.add_argument("--environment-name")
    return parser


def _print_build_results(results: list[dict[str, object]]) -> None:
    counts = Counter(str(result["status"]) for result in results)
    print("Building adapters...", flush=True)
    for result in results:
        duration = result.get("duration_seconds")
        duration_suffix = (
            f" ({float(duration):.2f}s)"
            if isinstance(duration, (int, float))
            else ""
        )
        print(
            f"  {result['adapter']}: {result['status']}{duration_suffix}",
            flush=True,
        )
    print(
        "Build summary: "
        f"{counts.get('built', 0)} built, "
        f"{counts.get('failed', 0)} failed, "
        f"{counts.get('skipped', 0)} skipped",
        flush=True,
    )


def _print_matrix_results(results: list[dict[str, object]]) -> None:
    total = len(results)
    passed = sum(1 for result in results if result["status"] == "passed")
    not_passed = total - passed
    print("Running conformance matrix...", flush=True)
    if total == 0:
        print("Matrix summary: 0 cases selected", flush=True)
        return
    print(
        f"Matrix summary: {passed} passed, {not_passed} did not pass, {total} total",
        flush=True,
    )

    by_scenario: dict[str, list[dict[str, object]]] = {}
    for result in results:
        by_scenario.setdefault(str(result["scenario_id"]), []).append(result)

    for scenario in ordered_scenarios():
        scenario_results = by_scenario.get(scenario.id)
        if not scenario_results:
            continue
        scenario_passed = sum(1 for result in scenario_results if result["status"] == "passed")
        scenario_not_passed = len(scenario_results) - scenario_passed
        print(
            f"  {scenario.display_name}: {scenario_passed} passed, "
            f"{scenario_not_passed} did not pass",
            flush=True,
        )

    remaining = [
        scenario_id
        for scenario_id in by_scenario
        if scenario_id not in {scenario.id for scenario in ordered_scenarios()}
    ]
    for scenario_id in sorted(remaining):
        scenario = require_scenario(scenario_id)
        scenario_results = by_scenario[scenario_id]
        scenario_passed = sum(1 for result in scenario_results if result["status"] == "passed")
        scenario_not_passed = len(scenario_results) - scenario_passed
        print(
            f"  {scenario.display_name}: {scenario_passed} passed, "
            f"{scenario_not_passed} did not pass",
            flush=True,
        )


def main() -> int:
    args = build_parser().parse_args()
    try:
        validate_repo_overrides()
    except RepoOverrideError as err:
        raise SystemExit(str(err)) from err
    results_dir = Path(args.results_dir)
    site_dir = Path(args.site_dir) if args.site_dir else results_dir
    environment = resolve_environment(
        environment_id=args.environment_id,
        environment_name=args.environment_name,
    )

    print(f"Results directory: {results_dir.resolve()}", flush=True)
    print(f"Environment: {environment.name} ({environment.id})", flush=True)
    build_results = build_selected_adapters(
        selected_adapters=selected_build_adapters(
            from_filter=args.from_filter,
            to_filter=args.to_filter,
        ),
    )
    annotated_build_results = annotate_build_results(
        build_results,
        environment_id=environment.id,
        environment_name=environment.name,
    )
    write_json(Path(args.build_report_path), {"results": annotated_build_results})
    _print_build_results(build_results)

    matrix_results = asyncio.run(
        run_matrix(
            results_dir=results_dir,
            from_filter=args.from_filter,
            to_filter=args.to_filter,
            timeout_s=args.timeout_seconds,
            jobs=args.jobs,
            build_results=build_results,
            environment_id=environment.id,
            environment_name=environment.name,
        )
    )
    _print_matrix_results(matrix_results)

    write_build_artifacts(
        results_dir / "data",
        build_results,
        environment_id=environment.id,
        environment_name=environment.name,
    )
    write_repository_versions(
        results_dir / "data",
        matrix_results,
        environment_id=environment.id,
        environment_name=environment.name,
    )
    print("Generating report site...", flush=True)
    build_site(results_dir, site_dir)
    print(f"Report written to {(site_dir / 'index.html').resolve()}", flush=True)
    del build_results
    del matrix_results
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
