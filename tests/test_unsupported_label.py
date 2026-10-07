"""Coverage for which failed cases the report labels unsupported.

The harness records that judgement on the result row when it decides, before
launching anything, that an implementation cannot run a scenario. The report
reads that field. An adapter's own wording never earns the label, whatever it
says.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conformance.io import read_json
from conformance.models import CaseResult
from conformance.runner import LaunchSpec, run_case
from conformance.site import _display_status, _status_counts

ADAPTER_WORDED_REASON = "sendspin-go server does not support server-initiated-pcm"

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


class DisplayStatusTest(unittest.TestCase):
    """The label a result row is rendered and counted under."""

    def test_a_failed_row_the_harness_marked_reads_unsupported(self) -> None:
        row = {"status": "failed", "reason": "anything", "unsupported": True}
        self.assertEqual(_display_status(row), "unsupported")

    def test_a_row_without_the_field_keeps_its_recorded_status(self) -> None:
        row = {"status": "failed", "reason": "sendspin-js is currently a client library."}
        self.assertEqual(_display_status(row), "failed")

    def test_adapter_wording_does_not_earn_the_label(self) -> None:
        for phrase in (
            "does not support",
            "currently a client library",
            "does not yet expose",
            "not a server implementation",
            "only supports client-initiated",
            "only supports server-initiated",
        ):
            with self.subTest(phrase=phrase):
                row = {
                    "status": "failed",
                    "reason": f"Server adapter reported: adapter {phrase} this.",
                    "unsupported": False,
                }
                self.assertEqual(_display_status(row), "failed")

    def test_only_a_boolean_true_counts(self) -> None:
        row = {"status": "failed", "reason": "", "unsupported": "true"}
        self.assertEqual(_display_status(row), "failed")

    def test_the_field_never_relabels_a_row_that_did_not_fail(self) -> None:
        row = {"status": "passed", "reason": "", "unsupported": True}
        self.assertEqual(_display_status(row), "passed")

    def test_counts_follow_the_field(self) -> None:
        counts = _status_counts(
            [
                {"status": "passed", "reason": ""},
                {"status": "failed", "reason": ADAPTER_WORDED_REASON},
                {"status": "failed", "reason": ADAPTER_WORDED_REASON, "unsupported": True},
            ]
        )
        self.assertEqual(counts, {"passed": 1, "failed": 1, "unsupported": 1})


class RunCaseUnsupportedTest(unittest.IsolatedAsyncioTestCase):
    """A case run end to end records whether the harness judged it unsupported."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.results_dir = Path(tmp.name)
        fixture = mock.patch("conformance.runner.fixture_path", return_value=Path("fixture.flac"))
        fixture.start()
        self.addCleanup(fixture.stop)

    async def _run(
        self,
        scenario_id: str,
        server_impl: str,
        client_impl: str,
        build_index: dict[str, dict[str, str]] | None = None,
    ) -> CaseResult:
        return await run_case(
            build_index=build_index,
            results_dir=self.results_dir,
            scenario_id=scenario_id,
            server_impl=server_impl,
            client_impl=client_impl,
            timeout_s=10.0,
            slot_index=0,
            environment_id="test",
            environment_name="Test",
        )

    def _launch(self, implementation, role, *, summary, ready, **_):
        if role == "server":
            argv = ["-c", FAILING_SERVER, str(ready), str(summary), ADAPTER_WORDED_REASON]
        else:
            argv = ["-c", CLIENT_WITHOUT_SUMMARY, str(ready)]
        return LaunchSpec(cmd=[sys.executable, *argv], cwd=self.results_dir)

    async def test_a_capability_the_role_lacks_is_recorded(self) -> None:
        result = await self._run("server-initiated-opus", "aiosendspin", "aiosendspin")

        self.assertEqual(result.status, "failed")
        self.assertTrue(result.unsupported)
        written = read_json(Path(result.case_dir) / "result.json")
        self.assertIs(written["unsupported"], True)
        self.assertEqual(_display_status(written), "unsupported")

    async def test_a_role_the_implementation_lacks_is_recorded(self) -> None:
        result = await self._run("client-initiated-pcm", "sendspin-js", "aiosendspin")

        self.assertEqual(result.status, "failed")
        self.assertTrue(result.unsupported)

    async def test_an_adapter_that_did_not_build_is_a_failure(self) -> None:
        failed_build = {"adapter": "python-adapters", "status": "failed", "detail": "error: x"}
        result = await self._run(
            "client-initiated-pcm",
            "aiosendspin",
            "aiosendspin",
            build_index={"python-adapters": failed_build},
        )

        self.assertEqual(result.status, "failed")
        self.assertFalse(result.unsupported)

    async def test_an_adapter_calling_itself_unsupported_is_a_failure(self) -> None:
        with mock.patch("conformance.runner._build_role_command", side_effect=self._launch):
            result = await self._run("server-initiated-pcm", "sendspin-go", "aiosendspin")

        self.assertEqual(result.status, "failed")
        self.assertIn("does not support", result.reason)
        self.assertFalse(result.unsupported)
        written = read_json(Path(result.case_dir) / "result.json")
        self.assertIs(written["unsupported"], False)
        self.assertEqual(_display_status(written), "failed")


if __name__ == "__main__":
    unittest.main()
