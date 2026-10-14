"""Writes a session's findings as `report.json`, `report.html` and a terminal summary."""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from .findings import Finding, Verdict
from .recorder import RecordedFrame

SPEC_TAG = "1.0.0-rc1"
REPORT_JSON = "report.json"
REPORT_HTML = "report.html"
PROTOCOL_LOG = "protocol.log.jsonl"
_SUMMARY_KEYS = {
    Verdict.PASS: "pass",
    Verdict.FAIL: "fail",
    Verdict.WARN: "warn",
    Verdict.SKIPPED: "skipped",
    Verdict.NOT_APPLICABLE: "n_a",
    Verdict.TOOL_ERROR: "tool_error",
}
_VENDORED_FIELD = re.compile(r"^- (Ref|Commit): `(.+)`$", re.MULTILINE)
_VENDORED_PATCH = re.compile(r"^  - `(.+\.patch)`$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    """Who the client said it was, from its client/init and client/hello."""

    name: str | None
    client_id: str | None
    device_info: dict[str, Any] | None
    supported_roles: list[Any]
    supported_formats: list[Any]

    @classmethod
    def from_frames(cls, frames: Sequence[RecordedFrame]) -> ClientIdentity:
        """Read the identity from the first client/init and client/hello in `frames`."""
        init = next((f for f in frames if f.inbound and f.type == "client/init"), None)
        hello = next((f for f in frames if f.inbound and f.type == "client/hello"), None)
        init_payload = init.payload if init is not None else None
        payload = (hello.payload if hello is not None else None) or {}
        player = payload.get("player@v1_support")
        formats = player.get("supported_formats") if isinstance(player, dict) else None
        name = payload.get("name")
        client_id = (init_payload or {}).get("client_id")
        device_info = payload.get("device_info")
        roles = payload.get("supported_roles")
        return cls(
            name=name if isinstance(name, str) else None,
            client_id=client_id if isinstance(client_id, str) else None,
            device_info=device_info if isinstance(device_info, dict) else None,
            supported_roles=roles if isinstance(roles, list) else [],
            supported_formats=formats if isinstance(formats, list) else [],
        )

    def to_json(self) -> dict[str, Any]:
        """Return the identity as `report.json` records it."""
        return {
            "client_id": self.client_id,
            "device_info": self.device_info or {},
            "name": self.name,
            "supported_formats": self.supported_formats,
            "supported_roles": self.supported_roles,
        }


def tool_version() -> str:
    """Return the validator's name and version, e.g. `sendspin-validate 0.1.0`."""
    return f"sendspin-validate {metadata.version('sendspin-conformance')}"


def bundled_aiosendspin() -> dict[str, Any]:
    """Return the upstream ref, commit and patches of the bundled aiosendspin."""
    text = (resources.files("conformance._vendor") / "VENDORED.md").read_text(encoding="utf-8")
    fields = dict(_VENDORED_FIELD.findall(text))
    return {
        "commit": fields.get("Commit"),
        "patches": _VENDORED_PATCH.findall(text),
        "ref": fields.get("Ref"),
    }


def sort_findings(findings: Sequence[Finding]) -> list[Finding]:
    """Return `findings` in report order: by id, then frames, then message."""
    return sorted(findings, key=lambda f: (f.id, f.frames, f.message, f.verdict))


def summarize(findings: Sequence[Finding]) -> dict[str, int]:
    """Count findings per verdict, keyed as `report.json` records it."""
    counts = Counter(f.verdict for f in findings)
    return {key: counts[verdict] for verdict, key in _SUMMARY_KEYS.items()}


def build_report(
    *,
    client: ClientIdentity,
    connection_mode: str,
    seed: int,
    findings: Sequence[Finding],
) -> dict[str, Any]:
    """Return the `report.json` document for a session."""
    ordered = sort_findings(findings)
    return {
        "aiosendspin": bundled_aiosendspin(),
        "client": client.to_json(),
        "connection_mode": connection_mode,
        "findings": [f.to_json() for f in ordered],
        "human_answers": [],
        "seed": seed,
        "spec": SPEC_TAG,
        "summary": summarize(ordered),
        "tool": tool_version(),
    }


def run_directory(parent: Path, client_name: str | None, now: datetime) -> Path:
    """
    Create and return `sendspin-validate-<client-name>-<timestamp>` under `parent`.

    The client name is reduced to characters safe in a file name.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", client_name or "").strip("-.") or "unknown-client"
    base = parent / f"sendspin-validate-{slug}-{now:%Y%m%d-%H%M%S}"
    directory, attempt = base, 1
    while True:
        try:
            directory.mkdir(parents=True)
        except FileExistsError:
            attempt += 1
            directory = base.with_name(f"{base.name}-{attempt}")
            continue
        return directory


def write_report(directory: Path, report: dict[str, Any]) -> None:
    """Write `report.json` and `report.html` into `directory`."""
    (directory / REPORT_JSON).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (directory / REPORT_HTML).write_text(render_html(report), encoding="utf-8")


def summary_lines(report: dict[str, Any]) -> list[str]:
    """Return the summary table printed at the end of a run."""
    summary = report["summary"]
    lines = ["", "Summary", f"  {'Verdict':<11}Count"]
    lines += [f"  {verdict.value:<11}{summary[key]}" for verdict, key in _SUMMARY_KEYS.items()]
    return lines


def render_html(report: dict[str, Any]) -> str:
    """Return `report` as a self-contained HTML page; every value is escaped."""
    client = report["client"]
    aiosendspin = report["aiosendspin"]
    details = [
        ("Client", client["name"]),
        ("Client id", client["client_id"]),
        ("Roles", ", ".join(map(str, client["supported_roles"]))),
        ("Connection mode", report["connection_mode"]),
        ("Spec", report["spec"]),
        ("Tool", report["tool"]),
        ("aiosendspin", f"{aiosendspin['ref']} ({aiosendspin['commit']})"),
        ("Patches", ", ".join(aiosendspin["patches"])),
        ("Seed", report["seed"]),
    ]
    summary = report["summary"]
    rows = "\n".join(
        '<tr class="{cls}"><td>{id}</td><td>{verdict}</td><td>{ref}</td><td>{message}</td>'
        "<td>{frames}</td></tr>".format(
            cls=_verdict_class(f["verdict"]),
            id=_e(f["id"]),
            verdict=_e(f["verdict"]),
            ref=_e(f["spec_ref"]),
            message=_e(f["message"]),
            frames=_e(", ".join(map(str, f["frames"]))),
        )
        for f in report["findings"]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sendspin validation report</title>
<style>
:root {{ --bg: #ffffff; --fg: #1d1d1f; --muted: #6e6e73; --line: #d2d2d7;
  --fail: #c62828; --warn: #a15c00; --pass: #2e7d32; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #161618; --fg: #f5f5f7; --muted: #a1a1a6; --line: #3a3a3c;
    --fail: #ef5350; --warn: #ffb74d; --pass: #81c784; }}
}}
body {{ margin: 0 auto; max-width: 1100px; padding: 24px 16px; background: var(--bg);
  color: var(--fg); font: 15px/1.5 -apple-system, system-ui, sans-serif; }}
h1 {{ font-size: 1.5em; margin: 0 0 16px; }}
table {{ border-collapse: collapse; width: 100%; margin: 0 0 24px; }}
th, td {{ border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left;
  vertical-align: top; overflow-wrap: anywhere; }}
th {{ color: var(--muted); font-weight: 600; }}
.details th {{ width: 12em; }}
.fail td:nth-child(2) {{ color: var(--fail); font-weight: 600; }}
.warn td:nth-child(2) {{ color: var(--warn); font-weight: 600; }}
.pass td:nth-child(2) {{ color: var(--pass); }}
.other td:nth-child(2) {{ color: var(--muted); }}
.scroll {{ overflow-x: auto; }}
</style>
</head>
<body>
<h1>Sendspin validation report</h1>
<table class="details">
{"".join(f"<tr><th>{_e(label)}</th><td>{_e(value)}</td></tr>" for label, value in details)}
</table>
<table>
<tr>{"".join(f"<th>{_e(v.value)}</th>" for v in _SUMMARY_KEYS)}</tr>
<tr>{"".join(f"<td>{summary[key]}</td>" for key in _SUMMARY_KEYS.values())}</tr>
</table>
<div class="scroll">
<table>
<tr><th>Check</th><th>Verdict</th><th>Spec</th><th>Finding</th><th>Frames</th></tr>
{rows}
</table>
</div>
</body>
</html>
"""


def _verdict_class(verdict: str) -> str:
    return {"FAIL": "fail", "TOOL_ERROR": "fail", "WARN": "warn", "PASS": "pass"}.get(
        verdict, "other"
    )


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))
