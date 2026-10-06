"""Coverage for the reason a failed case carries.

An adapter that fails writes its reason into its summary. The runner must carry
that reason into the result on every path, rather than an empty string, the
ready-file wait it gave up on, or a bare summary status.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conformance.io import write_json
from conformance.process import wait_for_exit
from conformance.runner import (
    CaseContext,
    LaunchSpec,
    _build_failure_reason,
    _compare_summaries,
    _incomplete_case_reason,
    run_case,
)
from conformance.scenarios import require_scenario

HANDSHAKE_REASON = "expected client/hello, got client/init"
UNSUPPORTED_REASON = "sendspin-go server does not support server-initiated-pcm-24bit"

# Stand-ins for an adapter CLI: each writes the files named on its command line.
FAILING_SERVER = """
import json, sys
ready, summary, reason = sys.argv[1:]
json.dump({"status": "ready"}, open(ready, "w"))
json.dump({"status": "error", "reason": reason}, open(summary, "w"))
sys.exit(1)
"""
CLIENT_WITHOUT_SUMMARY = """
import json, sys
json.dump({"status": "ready"}, open(sys.argv[1], "w"))
sys.exit(1)
"""


def _context(results_dir: Path, scenario_id: str) -> CaseContext:
    return CaseContext(
        results_dir=results_dir,
        environment_id="test",
        environment_name="Test",
        scenario=require_scenario(scenario_id),
        server_impl="sendspin-go",
        client_impl="aiosendspin",
        timeout_s=1.0,
        slot_index=0,
    )


class IncompleteCaseReasonTest(unittest.TestCase):
    """The reason for a case that ended without a usable summary from both adapters."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.results_dir = Path(tmp.name)

    def test_server_reason_leads_when_the_client_never_exits(self) -> None:
        context = _context(self.results_dir, "server-initiated-pcm")
        write_json(context.summary_path("server"), {"status": "error", "reason": HANDSHAKE_REASON})
        observed = "Timed out after 10s waiting for the client adapter to exit"
        reason = _incomplete_case_reason(context, observed)
        self.assertEqual(reason, f"Server adapter reported: {HANDSHAKE_REASON}; {observed}")

    def test_server_reason_leads_when_the_client_wrote_no_summary(self) -> None:
        context = _context(self.results_dir, "server-initiated-pcm")
        write_json(context.summary_path("server"), {"status": "error", "reason": HANDSHAKE_REASON})
        observed = (
            "Missing summary output for client: "
            "client adapter exited 1 before writing a summary"
        )
        reason = _incomplete_case_reason(context, observed)
        self.assertEqual(reason, f"Server adapter reported: {HANDSHAKE_REASON}; {observed}")

    def test_server_reason_leads_when_the_ready_file_never_appears(self) -> None:
        context = _context(self.results_dir, "server-initiated-pcm-24bit")
        write_json(
            context.summary_path("server"),
            {"status": "error", "reason": UNSUPPORTED_REASON},
        )
        observed = f"Timed out waiting for file: {context.ready_path('server')}"
        reason = _incomplete_case_reason(context, observed)
        self.assertEqual(
            reason,
            f"Server adapter reported: {UNSUPPORTED_REASON}; {observed}",
        )

    def test_harness_observation_stands_alone_without_a_summary(self) -> None:
        context = _context(self.results_dir, "server-initiated-pcm")
        observed = "Timed out after 10s waiting for the client adapter to exit"
        self.assertEqual(_incomplete_case_reason(context, observed), observed)

    def test_a_truncated_summary_is_not_read_as_a_reason(self) -> None:
        context = _context(self.results_dir, "server-initiated-pcm")
        context.case_dir.mkdir(parents=True)
        context.summary_path("server").write_text('{"status": "error", "rea', encoding="utf-8")
        self.assertEqual(_incomplete_case_reason(context, "gave up"), "gave up")


class RunCaseReasonTest(unittest.IsolatedAsyncioTestCase):
    """A case run end to end against stand-in adapters carries the server's reason."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.results_dir = Path(tmp.name)
        fixture = mock.patch("conformance.runner.fixture_path", return_value=Path("fixture.flac"))
        fixture.start()
        self.addCleanup(fixture.stop)

    def _launch(self, implementation, role, *, summary, ready, **_):
        if role == "server":
            argv = ["-c", FAILING_SERVER, str(ready), str(summary), HANDSHAKE_REASON]
        else:
            argv = ["-c", CLIENT_WITHOUT_SUMMARY, str(ready)]
        return LaunchSpec(cmd=[sys.executable, *argv], cwd=self.results_dir)

    async def _run(self):
        with mock.patch("conformance.runner._build_role_command", side_effect=self._launch):
            return await run_case(
                results_dir=self.results_dir,
                scenario_id="server-initiated-pcm",
                server_impl="sendspin-go",
                client_impl="aiosendspin",
                timeout_s=10.0,
                slot_index=0,
                environment_id="test",
                environment_name="Test",
            )

    async def test_client_exiting_without_a_summary(self) -> None:
        result = await self._run()
        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.reason,
            f"Server adapter reported: {HANDSHAKE_REASON}; Missing summary output for client: "
            "client adapter exited 1 before writing a summary",
        )

    async def test_a_wait_that_fails_without_a_message(self) -> None:
        with mock.patch("conformance.runner.wait_for_exit", side_effect=TimeoutError()):
            result = await self._run()
        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.reason,
            f"Server adapter reported: {HANDSHAKE_REASON}; TimeoutError",
        )


class ComparisonReasonTest(unittest.TestCase):
    """The reason for a case where both adapters finished and one reported an error."""

    def test_both_adapter_reasons_are_reported_with_their_roles(self) -> None:
        matches, reason = _compare_summaries(
            require_scenario("client-initiated-pcm"),
            {"status": "error", "reason": HANDSHAKE_REASON},
            {"status": "error", "reason": "expected server/init (TEXT), got CLOSED"},
        )
        self.assertFalse(matches)
        self.assertEqual(
            reason,
            f"Server adapter reported: {HANDSHAKE_REASON}; "
            "Client adapter reported: expected server/init (TEXT), got CLOSED",
        )

    def test_a_client_only_failure_is_reported(self) -> None:
        matches, reason = _compare_summaries(
            require_scenario("client-initiated-pcm"),
            {"status": "ok"},
            {"status": "error", "reason": "expected server/init (TEXT), got CLOSED"},
        )
        self.assertFalse(matches)
        self.assertEqual(reason, "Client adapter reported: expected server/init (TEXT), got CLOSED")

    def test_an_error_summary_without_a_reason_still_names_the_role(self) -> None:
        matches, reason = _compare_summaries(
            require_scenario("client-initiated-pcm"),
            {"status": "error"},
            {"status": "ok"},
        )
        self.assertFalse(matches)
        self.assertEqual(reason, "Server summary status is 'error'")


class BuildFailureReasonTest(unittest.TestCase):
    """The reason for a case whose adapter never built."""

    def test_leads_with_the_compiler_diagnostic_from_a_truncated_log(self) -> None:
        detail = (
            "ng version_check v0.9.5\n"
            "   Compiling thiserror v2.0.18\n"
            "error[E0432]: unresolved import `sendspin::Client`\n"
            "error: could not compile `adapter` due to 1 previous error\n"
        )
        self.assertEqual(
            _build_failure_reason({"adapter": "x-client", "status": "failed", "detail": detail}),
            "x-client build failed: error[E0432]: unresolved import `sendspin::Client`",
        )

    def test_a_dependency_named_after_errors_is_not_a_diagnostic(self) -> None:
        detail = (
            "   Compiling quick-error v2.0.1\n"
            "   Compiling thiserror v2.0.18\n"
            "main.cpp:468:58: error: only virtual member functions can be marked 'override'\n"
        )
        self.assertEqual(
            _build_failure_reason({"adapter": "x-client", "status": "failed", "detail": detail}),
            "x-client build failed: main.cpp:468:58: error: "
            "only virtual member functions can be marked 'override'",
        )

    def test_falls_back_to_the_first_line_without_a_diagnostic(self) -> None:
        detail = "Unable to locate a Java Runtime.\nPlease visit http://www.java.com\n"
        self.assertEqual(
            _build_failure_reason({"adapter": "x-client", "status": "failed", "detail": detail}),
            "x-client build failed: Unable to locate a Java Runtime.",
        )

    def test_says_so_when_no_detail_was_recorded(self) -> None:
        self.assertEqual(
            _build_failure_reason({"adapter": "x-client", "status": "failed"}),
            "x-client build failed: no detail available",
        )


class WaitForExitTest(unittest.IsolatedAsyncioTestCase):
    """A process that outlives its timeout produces a reason naming the adapter role."""

    async def test_timeout_names_the_role(self) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)"
        )
        try:
            with self.assertRaises(TimeoutError) as raised:
                await wait_for_exit(process, role="client", timeout_s=0.1)
        finally:
            process.kill()
            await process.wait()
        self.assertEqual(
            str(raised.exception),
            "Timed out after 0.1s waiting for the client adapter to exit",
        )

    async def test_an_exited_process_returns(self) -> None:
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
        await wait_for_exit(process, role="server", timeout_s=10)
        self.assertEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
