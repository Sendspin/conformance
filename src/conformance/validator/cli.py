"""Command line entry point for `sendspin-validate`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import sys
from datetime import datetime
from pathlib import Path

from .checks import judge_session
from .prompter import AutoAnswerPrompter, Prompter, TerminalPrompter
from .recorder import Recorder
from .report import (
    PROTOCOL_LOG,
    SPEC_TAG,
    ClientIdentity,
    build_report,
    run_directory,
    summary_lines,
    tool_version,
    write_report,
)
from .session import PortUnavailableError, SessionOptions, SessionResult, run_session
from .storage import load_state, user_data_dir
from .suites import Suite, parse_suites

DEFAULT_PORT = 8927
# Exit statuses: every finding is clean, a FAIL or TOOL_ERROR was found, no report.
EXIT_CLEAN, EXIT_FINDINGS, EXIT_NO_REPORT = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    """Create the `sendspin-validate` argument parser."""
    parser = argparse.ArgumentParser(
        prog="sendspin-validate",
        description="Check a Sendspin client app against the spec by letting it connect "
        "to a strict Sendspin server.",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="port to listen on (default: 8927)"
    )
    parser.add_argument(
        "--connect",
        metavar="URL",
        help="connect to the client at this ws:// URL instead of using mDNS",
    )
    parser.add_argument("--fresh", action="store_true", help="start from a new validator identity")
    parser.add_argument(
        "--verbose", action="store_true", help="stream every decoded frame to stderr"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(),
        help="directory to write the run directory into (default: the current directory)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="how long to wait for a client to connect or reconnect (default: 300)",
    )
    parser.add_argument(
        "--suite",
        type=_suites,
        default=frozenset(Suite),
        metavar="NAMES",
        help="comma-separated suites to run after the opening checks, in this order: "
        f"{', '.join(Suite)} (default: all)",
    )
    parser.add_argument(
        "--fault-injection",
        action="store_true",
        help="also run steps that send what no client may accept, such as U4",
    )
    # For CI only: answers every question with the answer the session expects.
    parser.add_argument("--auto-answer", action="store_true", help=argparse.SUPPRESS)
    # For CI only: the pairing token --auto-answer pastes, which only the test knows.
    parser.add_argument("--auto-answer-token", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """
    Run one validation session and return the exit status.

    0 when no finding is FAIL or TOOL_ERROR, 1 when one is, 2 when no report was written.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    prompter: Prompter = (
        AutoAnswerPrompter(secret_answer=args.auto_answer_token)
        if args.auto_answer
        else TerminalPrompter()
    )
    prompter.instruct(f"Sendspin Validator {tool_version().split()[-1]} — spec {SPEC_TAG}")
    recorder = Recorder(verbose=sys.stderr if args.verbose else None)
    if args.auto_answer_token:
        recorder.add_secret(args.auto_answer_token)
    options = SessionOptions(
        port=args.port,
        connect_url=args.connect,
        connect_timeout_s=args.timeout,
        suites=args.suite,
        fault_injection=args.fault_injection,
    )
    try:
        result = asyncio.run(_session(prompter, recorder, options, fresh=args.fresh))
    except (EOFError, KeyboardInterrupt):
        prompter.instruct("\nStopped before the run finished; no report was written.")
        return EXIT_NO_REPORT
    except PortUnavailableError as err:
        prompter.instruct(f"Could not listen on port {args.port}: {err.__cause__ or err}")
        return EXIT_NO_REPORT
    if result is None:
        return EXIT_NO_REPORT
    return _report(prompter, recorder, result, args.output_dir)


async def _session(
    prompter: Prompter, recorder: Recorder, options: SessionOptions, *, fresh: bool
) -> SessionResult | None:
    identity, pairing_store = await load_state(user_data_dir(), fresh=fresh)
    return await run_session(prompter, recorder, identity, pairing_store, options)


def _report(prompter: Prompter, recorder: Recorder, result: SessionResult, output_dir: Path) -> int:
    """Judge the observed connection, write the run directory, and return the exit status."""
    opening = judge_session(
        recorder.frames,
        recorder.noncompliance,
        recorder.tool_errors,
        connection=result.connection,
        later_connections=result.later_connections,
        txt_name=result.txt_name,
    )
    prompter.instruct("\nOpening exchange")
    for finding in opening:
        prompter.report_check(finding)
    findings = [*opening, *result.findings]
    judged = {result.connection, *result.later_connections}
    ignored = sorted({f.connection for f in recorder.frames} - judged)
    if ignored:
        prompter.instruct(
            f"Not judged: {len(ignored)} other connection(s), from clients other than the "
            "one under test. Their frames are still in protocol.log.jsonl."
        )
    observed = [f for f in recorder.frames if f.connection == result.connection]
    client = ClientIdentity.from_frames(observed)
    report = build_report(
        client=client,
        connection_mode=result.mode.value,
        seed=secrets.randbelow(2**32),
        findings=findings,
    )
    directory = run_directory(output_dir, client.name, datetime.now())
    write_report(directory, report)
    recorder.write_log(directory / PROTOCOL_LOG)
    for line in summary_lines(report):
        prompter.instruct(line)
    prompter.instruct(f"\nReport written to {directory}")
    summary = report["summary"]
    return EXIT_FINDINGS if summary["fail"] or summary["tool_error"] else EXIT_CLEAN


def _suites(text: str) -> frozenset[Suite]:
    try:
        return parse_suites(text)
    except ValueError as err:
        raise argparse.ArgumentTypeError(str(err)) from None


if __name__ == "__main__":
    sys.exit(main())
