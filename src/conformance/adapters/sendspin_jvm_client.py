"""sendspin-jvm client adapter for the conformance harness."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from conformance.implementations import resolve_required_repo_path
from conformance.toolchains import find_java


def _jar_path() -> Path:
    repo = resolve_required_repo_path("sendspin-jvm")
    return repo / "conformance-client" / "build" / "libs" / "conformance-client.jar"


def main() -> None:
    jar = _jar_path()
    if not jar.exists():
        print(
            f"conformance-client.jar not found at {jar}\n"
            "Build it first: ./gradlew :conformance-client:jar",
            file=sys.stderr,
        )
        sys.exit(1)

    java = find_java()
    if java is None:
        print("No java executable found. Set JAVA_HOME or add java to PATH.", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run([java, "-jar", str(jar)] + sys.argv[1:], check=False)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
