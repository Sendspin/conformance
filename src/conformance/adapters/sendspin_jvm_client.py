"""Conformance adapter for sendspin-jvm (JVM client).

Drop this file into the conformance harness at:
  src/conformance/adapters/sendspin_jvm_client.py

Then add an entry to src/conformance/implementations.py — see the project README
or the CI workflow for the exact entry.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


# Resolve the JAR relative to this file's location inside the conformance harness tree.
# Expected layout (harness cloned alongside our repo):
#   <workspace>/sendspin-jvm/   ← our repo
#   <workspace>/conformance/    ← harness repo
# This file lives at: <workspace>/conformance/src/conformance/adapters/
# parents[4]        = <workspace>/
JAR_PATH = (
    Path(__file__).resolve().parents[4]
    / "sendspin-jvm"
    / "conformance-client"
    / "build"
    / "libs"
    / "conformance-client.jar"
)

# Prefer a real JVM over the macOS stub at /usr/bin/java.
_JAVA_CANDIDATES = [
    # Android Studio bundled JBR (macOS)
    "/Applications/Android Studio.app/Contents/jbr/Contents/Home/bin/java",
    # Standard JAVA_HOME
    *([str(Path(__import__("os").environ["JAVA_HOME"]) / "bin" / "java")]
      if "JAVA_HOME" in __import__("os").environ else []),
]


def _find_java() -> str:
    for candidate in _JAVA_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("java")
    if found:
        return found
    raise FileNotFoundError(
        "No working Java runtime found. Checked: " + ", ".join(_JAVA_CANDIDATES)
    )


def main() -> None:
    if not JAR_PATH.exists():
        print(
            f"conformance-client.jar not found at {JAR_PATH}\n"
            "Build it first: ./gradlew :conformance-client:jar",
            file=sys.stderr,
        )
        sys.exit(1)

    java = _find_java()
    result = subprocess.run(
        [java, "-jar", str(JAR_PATH)] + sys.argv[1:],
        check=False,
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
