"""The validator's report is complete, deterministic and safe to open."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from conformance.validator.findings import Finding, Verdict
from conformance.validator.recorder import RecordedFrame
from conformance.validator.report import (
    ClientIdentity,
    build_report,
    render_html,
    run_directory,
    summary_lines,
    write_report,
)

FINDINGS = [
    Finding("CORE-TIME-001", Verdict.PASS, "messaging.md:281", "ok", frames=(9,)),
    Finding("CORE-HELLO-007", Verdict.WARN, "connection.md:19", "<b>names</b> differ"),
    Finding("CORE-HELLO-001", Verdict.FAIL, "messaging.md:198", "no hello", frames=(5,)),
    Finding("CORE-HELLO-001", Verdict.FAIL, "messaging.md:198", "no hello", frames=(2,)),
]


def hello_frames(name: str) -> list[RecordedFrame]:
    common = {"connection": 1, "client_id": "id", "direction": "in", "monotonic_us": 0, "size": 0}
    return [
        RecordedFrame(
            id=1,
            phase="cleartext",
            message={"type": "client/init", "payload": {"client_id": "abc"}},
            **common,
        ),
        RecordedFrame(
            id=2,
            phase="transport",
            message={
                "type": "client/hello",
                "payload": {
                    "name": name,
                    "supported_roles": ["player@v1"],
                    "player@v1_support": {"supported_formats": [{"codec": "pcm"}]},
                },
            },
            **common,
        ),
    ]


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ClientIdentity.from_frames(hello_frames("Kitchen <Speaker>"))
        self.report = build_report(
            client=self.client, connection_mode="server-initiated", seed=7, findings=FINDINGS
        )

    def test_records_provenance_and_identity(self) -> None:
        self.assertEqual(self.report["spec"], "1.0.0-rc1")
        self.assertRegex(self.report["tool"], r"^sendspin-validate \d")
        self.assertEqual(self.report["aiosendspin"]["ref"], "10.0.2")
        self.assertRegex(self.report["aiosendspin"]["commit"], r"^[0-9a-f]{40}$")
        self.assertIn("0001-frame-observer.patch", self.report["aiosendspin"]["patches"])
        self.assertEqual(self.report["seed"], 7)
        self.assertEqual(self.report["connection_mode"], "server-initiated")
        self.assertEqual(
            self.report["client"],
            {
                "client_id": "abc",
                "device_info": {},
                "name": "Kitchen <Speaker>",
                "supported_formats": [{"codec": "pcm"}],
                "supported_roles": ["player@v1"],
            },
        )

    def test_findings_are_sorted_and_counted(self) -> None:
        self.assertEqual(
            [(f["id"], f["frames"]) for f in self.report["findings"]],
            [
                ("CORE-HELLO-001", [2]),
                ("CORE-HELLO-001", [5]),
                ("CORE-HELLO-007", []),
                ("CORE-TIME-001", [9]),
            ],
        )
        self.assertEqual(
            self.report["summary"],
            {"pass": 1, "fail": 2, "warn": 1, "skipped": 0, "n_a": 0, "tool_error": 0},
        )
        self.assertIn("  FAIL       2", summary_lines(self.report))

    def test_written_report_is_deterministic(self) -> None:
        reordered = build_report(
            client=self.client,
            connection_mode="server-initiated",
            seed=7,
            findings=list(reversed(FINDINGS)),
        )
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            write_report(Path(first), self.report)
            write_report(Path(second), reordered)
            for name in ("report.json", "report.html"):
                self.assertEqual(
                    (Path(first) / name).read_text(), (Path(second) / name).read_text(), name
                )
            text = (Path(first) / "report.json").read_text()
            self.assertEqual(text, json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n")

    def test_html_escapes_client_supplied_text(self) -> None:
        page = render_html(self.report)
        self.assertNotIn("<Speaker>", page)
        self.assertIn("Kitchen &lt;Speaker&gt;", page)
        self.assertNotIn("<b>names</b>", page)
        self.assertNotIn("<script src", page)


class RunDirectoryTests(unittest.TestCase):
    def test_name_is_safe_and_unique(self) -> None:
        now = datetime(2026, 10, 14, 9, 5, 7)
        with tempfile.TemporaryDirectory() as parent:
            first = run_directory(Path(parent), "../Kitchen Speaker!", now)
            second = run_directory(Path(parent), "../Kitchen Speaker!", now)
            unnamed = run_directory(Path(parent), None, now)
            self.assertEqual(first.name, "sendspin-validate-Kitchen-Speaker-20261014-090507")
            self.assertEqual(second.name, f"{first.name}-2")
            self.assertEqual(unnamed.name, "sendspin-validate-unknown-client-20261014-090507")
            self.assertTrue(all(p.parent == Path(parent) for p in (first, second, unnamed)))


if __name__ == "__main__":
    unittest.main()
