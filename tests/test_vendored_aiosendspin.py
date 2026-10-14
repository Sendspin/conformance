"""The bundled aiosendspin stays apart from the aiosendspin the harness loads."""

import re
import subprocess
import sys
import unittest
from pathlib import Path

import conformance._vendor

VENDOR_DIR = Path(conformance._vendor.__file__).parent
TOP_LEVEL_REFERENCE = re.compile(
    r"^\s*(?:from|import)\s+aiosendspin\b|[\"']aiosendspin[.\"']", re.MULTILINE
)


class VendoredAiosendspinTests(unittest.TestCase):
    def test_server_import_does_not_load_top_level_aiosendspin(self):
        # A fresh interpreter, so modules other tests imported cannot mask a leak.
        probe = (
            "import sys\n"
            "import conformance._vendor.aiosendspin.server\n"
            "print(sorted(m for m in sys.modules if m.split('.')[0] == 'aiosendspin'))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "[]")

    def test_no_bundled_module_references_top_level_aiosendspin(self):
        offenders = [
            f"{path.relative_to(VENDOR_DIR)}: {match.group(0).strip()}"
            for path in sorted(VENDOR_DIR.rglob("*.py"))
            for match in TOP_LEVEL_REFERENCE.finditer(path.read_text())
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
