#!/usr/bin/env python3
"""Bootstrap a local development environment for the conformance repo."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from conformance.implementations import resolve_repo_path, validate_repo_overrides
from conformance.paths import RepoOverrideError, repo_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clone", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        validate_repo_overrides()
    except RepoOverrideError as err:
        raise SystemExit(str(err)) from err
    root = repo_root()

    if args.clone:
        subprocess.run([sys.executable, str(root / "scripts" / "setup_repositories.py")], check=True)

    subprocess.run(["uv", "sync"], cwd=str(root), check=True)

    aiosendspin_repo = resolve_repo_path("aiosendspin")
    if aiosendspin_repo is not None:
        subprocess.run(["uv", "pip", "install", "-e", str(aiosendspin_repo)], check=True)

    print("Environment ready (uv sync complete)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
