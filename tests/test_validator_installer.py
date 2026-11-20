"""The sendspin-validate installer is valid shell and pins what uv.lock pins."""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-sendspin-validate.sh"
CONSTRAINTS = ROOT / "scripts" / "sendspin-validate-constraints.txt"
# The command the constraints file's header records, run from the repository root.
EXPORT = [
    "uv",
    "export",
    "--frozen",
    "--no-hashes",
    "--no-emit-project",
    "--no-dev",
    "--no-annotate",
]


class InstallerTests(unittest.TestCase):
    def test_the_installer_is_valid_posix_shell(self) -> None:
        subprocess.run(["sh", "-n", str(INSTALLER)], check=True)

    @unittest.skipUnless(shutil.which("uv"), "uv is not installed")
    def test_the_constraints_match_uv_lock(self) -> None:
        exported = subprocess.run(
            EXPORT, cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout
        pinned = [line for line in CONSTRAINTS.read_text().splitlines() if not line.startswith("#")]
        self.assertEqual(
            pinned,
            [line for line in exported.splitlines() if not line.startswith("#")],
            "uv.lock changed: regenerate scripts/sendspin-validate-constraints.txt with the "
            "command in its header",
        )


if __name__ == "__main__":
    unittest.main()
