"""Coverage for the formats the aiosendspin client adapter is willing to advertise.

`roles/player/v1.md:20` requires a player to list `pcm` or `flac`, and may list
`opus` in addition. The client SDK decodes only PCM and FLAC and rejects a
`supported_formats` list holding anything else, so this adapter cannot satisfy
both at once and must refuse rather than advertise an opus-only list. The
registry marks the client `supports_opus=False`, which fail-fasts the opus case
before the adapter is launched, so the matrix never reaches this rule.
"""

from __future__ import annotations

import unittest

from conformance.adapters.aiosendspin_client import _supported_formats


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


if __name__ == "__main__":
    unittest.main()
