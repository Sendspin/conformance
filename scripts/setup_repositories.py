#!/usr/bin/env python3
"""Clone the implementation repositories used by the conformance suite."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from conformance.implementations import IMPLEMENTATIONS, SUPPORTING_REPOS
from conformance.paths import repo_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos-dir", default=str(repo_root() / "repos"))
    parser.add_argument("--update", action="store_true")
    return parser


# Pin specific repositories to a fixed revision. Used when an implementation
# lands breaking changes the conformance adapters have not been migrated to yet;
# remove an entry once the adapters catch up.
REPO_PINS: dict[str, str] = {
    # Remove once aiosendspin works with non-encrypted clients again
    "aiosendspin": "af9dbc8b625bffbee99d7a67712c0e3b4c44147d",
}


def clone_or_update(
    target: Path, url: str, update: bool, pin: str | None = None
) -> None:
    if not target.exists():
        subprocess.run(["git", "clone", url, str(target)], check=True)
    if pin:
        # Pinned repository: fetch and check out the exact revision instead of
        # tracking the default branch.
        subprocess.run(["git", "-C", str(target), "fetch", "origin"], check=True)
        subprocess.run(["git", "-C", str(target), "checkout", pin], check=True)
        return
    if update:
        subprocess.run(["git", "-C", str(target), "pull", "--ff-only"], check=True)


def main() -> int:
    args = build_parser().parse_args()
    repos_dir = Path(args.repos_dir)
    repos_dir.mkdir(parents=True, exist_ok=True)

    for spec in IMPLEMENTATIONS.values():
        clone_or_update(
            repos_dir / spec.repo_dirname,
            spec.remote_url,
            args.update,
            pin=REPO_PINS.get(spec.repo_dirname),
        )
    for dirname, url in SUPPORTING_REPOS.values():
        clone_or_update(
            repos_dir / dirname, url, args.update, pin=REPO_PINS.get(dirname)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
