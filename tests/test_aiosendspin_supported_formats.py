"""Coverage for the formats the aiosendspin client adapter will advertise, and
for the contract it must honour when it refuses.

`roles/player/v1.md:20` requires a player to list `pcm` or `flac`, and may list
`opus` in addition. The client SDK decodes only PCM and FLAC and rejects a
`supported_formats` list holding anything else, so this adapter cannot list
opus ahead of a pcm or flac entry and must refuse rather than advertise a codec
it cannot handle. The registry marks the client `supports_opus=False`, which
fail-fasts the opus case before the adapter is launched, so the matrix never
reaches this rule.

A refusal still has to reach the harness as a result. AGENTS.md requires a
client-side case that cannot run to fail fast, emit a summary and exit non-zero.
`_supported_formats()` is called outside `_run()`'s `try`, so a refusal that
escaped it would leave the harness waiting on a ready file that never arrives,
turning a reportable reason into a timeout.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from conformance.adapters.aiosendspin_client import _run, _supported_formats, build_parser
from conformance.implementations import resolve_repo_path


def _client_args(tmp: Path, *, preferred_codec: str) -> Any:
    return build_parser().parse_args(
        [
            "--client-name", "aiosendspin-client",
            "--client-id", "client",
            "--summary", str(tmp / "summary.json"),
            "--ready", str(tmp / "ready.json"),
            "--registry", str(tmp / "registry.json"),
            "--scenario-id", "server-initiated-flac",
            "--preferred-codec", preferred_codec,
        ]
    )


class SupportedFormatsTests(unittest.TestCase):
    def test_opus_is_refused_rather_than_advertised(self) -> None:
        with self.assertRaises(ValueError) as caught:
            _supported_formats("opus")
        self.assertIn("opus", str(caught.exception))

    def test_unknown_codec_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _supported_formats("vorbis")

    def test_decodable_codecs_are_advertised(self) -> None:
        for codec in ("pcm", "flac"):
            with self.subTest(codec=codec):
                formats = _supported_formats(codec)
                self.assertTrue(formats)
                self.assertEqual({fmt.codec.value for fmt in formats}, {codec})


@unittest.skipUnless(
    resolve_repo_path("aiosendspin") is not None,
    "needs the aiosendspin checkout that the adapter adds to sys.path",
)
class RefusedFormatsReportAResultTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_refusal_is_reported(self, preferred_codec: str) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            exit_code = await _run(_client_args(tmp, preferred_codec=preferred_codec))
            ready = json.loads((tmp / "ready.json").read_text())
            summary = json.loads((tmp / "summary.json").read_text())
        self.assertEqual(exit_code, 1)
        self.assertEqual(ready["status"], "error")
        self.assertEqual(summary["status"], "error")
        self.assertIn(preferred_codec, summary["reason"])

    async def test_opus_refusal_writes_ready_and_summary(self) -> None:
        await self._assert_refusal_is_reported("opus")

    async def test_unknown_codec_refusal_writes_ready_and_summary(self) -> None:
        await self._assert_refusal_is_reported("vorbis")


if __name__ == "__main__":
    unittest.main()
