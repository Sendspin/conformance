"""Top-level CLI for the conformance harness."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .build import build_adapters, build_failed
from .implementations import validate_repo_overrides
from .paths import RepoOverrideError
from .runner import run_matrix
from .site import build_site


def _print_build_results(results: list[dict[str, object]]) -> None:
    for result in results:
        duration = result.get("duration_seconds")
        duration_suffix = (
            f" ({float(duration):.2f}s)"
            if isinstance(duration, (int, float))
            else ""
        )
        print(f"{result['adapter']}: {result['status']}{duration_suffix}")
        detail = result["detail"].strip()
        if detail:
            print(detail)


def _print_case_results(results: list[dict[str, object]]) -> None:
    for result in results:
        print(
            "{status}: {scenario} :: {server} -> {client} :: {reason}".format(
                status=result["status"],
                scenario=result["scenario_id"],
                server=result["server_impl"],
                client=result["client_impl"],
                reason=result["reason"],
            )
        )


def build_parser() -> argparse.ArgumentParser:
    """Create the root CLI parser."""
    parser = argparse.ArgumentParser(prog="conformance")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Build adapter sources")
    build_parser.add_argument("--report-path")

    run_parser = subparsers.add_parser("run", help="Run the current matrix")
    run_parser.add_argument("--results-dir", default="results")
    run_parser.add_argument("--from", dest="from_filter")
    run_parser.add_argument("--to", dest="to_filter")
    run_parser.add_argument("--timeout-seconds", type=float, default=40.0)
    run_parser.add_argument("--jobs", type=int, default=1)
    run_parser.add_argument("--environment-id")
    run_parser.add_argument("--environment-name")

    report_parser = subparsers.add_parser("report", help="Generate a static report site")
    report_parser.add_argument("--results-dir", default="results")
    report_parser.add_argument("--site-dir")
    return parser


def main() -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command in ("build", "run"):
            validate_repo_overrides()
        if args.command == "build":
            results = build_adapters(Path(args.report_path) if args.report_path else None)
            _print_build_results(results)
            return 1 if build_failed(results) else 0
        if args.command == "run":
            results = asyncio.run(
                run_matrix(
                    results_dir=Path(args.results_dir),
                    from_filter=args.from_filter,
                    to_filter=args.to_filter,
                    timeout_s=args.timeout_seconds,
                    jobs=args.jobs,
                    environment_id=args.environment_id,
                    environment_name=args.environment_name,
                )
            )
            _print_case_results(results)
            return 1 if any(result["status"] == "failed" for result in results) else 0
        if args.command == "report":
            site_dir = Path(args.site_dir) if args.site_dir else Path(args.results_dir)
            build_site(Path(args.results_dir), site_dir)
            print(f"Report written to {(site_dir / 'index.html').resolve()}")
            return 0
    except (FileNotFoundError, RepoOverrideError, ValueError) as err:
        parser.error(str(err))
    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
