#!/usr/bin/env python3
"""
Build the bundled aiosendspin under src/conformance/_vendor/.

Exports the aiosendspin package from upstream at a ref, applies
patches/aiosendspin/*.patch in name order, and rewrites its imports to
conformance._vendor.aiosendspin. The bundled tree is generated, never edited by
hand: every change to it goes through a patch.

    python scripts/vendor_aiosendspin.py 10.0.2    # rebuild at a tag, branch or commit
    python scripts/vendor_aiosendspin.py --check   # verify the committed tree

--check rebuilds at the commit VENDORED.md records and exits 1 when the result
differs from the committed tree in any byte.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "src" / "conformance" / "_vendor"
PATCH_DIR = ROOT / "patches" / "aiosendspin"
UPSTREAM_URL = "https://github.com/Sendspin/aiosendspin"
VENDORED_PACKAGE = "conformance._vendor.aiosendspin"

_FROM_IMPORT = re.compile(r"^([ \t]*)from aiosendspin(?=[.\s])", re.MULTILINE)
_PLAIN_IMPORT = re.compile(r"^([ \t]*)import aiosendspin\b(.*)$", re.MULTILINE)
_RECORDED_REF = re.compile(r"^- Ref: `(.+)`$", re.MULTILINE)
_RECORDED_COMMIT = re.compile(r"^- Commit: `([0-9a-f]{40})`$", re.MULTILINE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("ref", nargs="?", help="upstream tag, branch or commit to vendor")
    parser.add_argument(
        "--check",
        action="store_true",
        help="rebuild at the recorded commit and fail if the committed tree differs",
    )
    return parser


def build(ref: str, dest: Path, *, commit: str | None = None) -> None:
    """
    Write the complete _vendor tree for upstream `ref` into `dest`.

    `commit` pins the export to that commit while VENDORED.md still records
    `ref`. Raises SystemExit when the export fails, a patch does not apply, or
    pyproject.toml lacks a dependency the bundled copy declares.
    """
    patches = sorted(PATCH_DIR.glob("*.patch"))
    with tempfile.TemporaryDirectory() as work:
        checkout = Path(work)
        exported = _export(commit or ref, checkout)
        if commit is not None and exported != commit:
            raise SystemExit(f"fetched {exported} for pinned commit {commit}")
        for patch in patches:
            _apply_patch(patch, checkout)
        missing = missing_dependencies(checkout / "pyproject.toml")
        if missing:
            raise SystemExit(
                f"pyproject.toml lacks dependencies aiosendspin {ref} declares: {', '.join(missing)}"
            )

        dest.mkdir(parents=True, exist_ok=True)
        (dest / "__init__.py").write_text(
            '"""Third-party packages bundled by scripts/vendor_aiosendspin.py."""\n'
        )
        package = dest / "aiosendspin"
        for source in sorted((checkout / "aiosendspin").rglob("*")):
            if source.is_dir() or "__pycache__" in source.parts:
                continue
            target = package / source.relative_to(checkout / "aiosendspin")
            target.parent.mkdir(parents=True, exist_ok=True)
            data = source.read_bytes()
            if source.suffix == ".py":
                data = rewrite_imports(data.decode("utf-8"), source).encode("utf-8")
            target.write_bytes(data)
        shutil.copyfile(checkout / "LICENSE", package / "LICENSE")
        (dest / "VENDORED.md").write_text(_vendored_md(ref, exported, patches))


def rewrite_imports(text: str, source: Path) -> str:
    """
    Return `text` with every aiosendspin import pointed at the bundled package.

    Raises SystemExit on a bare `import aiosendspin...` without `as`, whose
    rewrite would change the name the module binds.
    """

    def plain(match: re.Match[str]) -> str:
        if " as " not in match.group(2):
            raise SystemExit(f"{source}: cannot rewrite {match.group(0).strip()!r} without 'as'")
        return f"{match.group(1)}import {VENDORED_PACKAGE}{match.group(2)}"

    text = _FROM_IMPORT.sub(rf"\1from {VENDORED_PACKAGE}", text)
    return _PLAIN_IMPORT.sub(plain, text)


def missing_dependencies(upstream_pyproject: Path) -> list[str]:
    """
    Return the upstream runtime and server requirements pyproject.toml does not declare.

    A requirement counts as declared only with the same specifier, ignoring case
    and whitespace, so a range upstream tightened is reported too.
    """
    upstream = tomllib.loads(upstream_pyproject.read_text())["project"]
    required = [*upstream["dependencies"], *upstream["optional-dependencies"]["server"]]
    ours = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    declared = {_normalise_requirement(requirement) for requirement in ours}
    return [
        requirement
        for requirement in required
        if _normalise_requirement(requirement) not in declared
    ]


def recorded_source() -> tuple[str, str]:
    """
    Return the `(ref, commit)` the committed VENDORED.md records.

    Raises SystemExit when the file is missing or does not record both.
    """
    path = VENDOR_DIR / "VENDORED.md"
    if not path.is_file():
        raise SystemExit(f"{path.relative_to(ROOT)} is missing; vendor a ref first")
    text = path.read_text()
    ref = _RECORDED_REF.search(text)
    commit = _RECORDED_COMMIT.search(text)
    if ref is None or commit is None:
        raise SystemExit(f"{path.relative_to(ROOT)} does not record a ref and commit")
    return ref.group(1), commit.group(1)


def tree_differences(expected: Path, actual: Path) -> list[str]:
    """Return the relative paths whose presence or bytes differ, ignoring bytecode caches."""
    expected_files = _files(expected)
    actual_files = _files(actual)
    return sorted(
        path
        for path in expected_files | actual_files
        if path not in expected_files
        or path not in actual_files
        or (expected / path).read_bytes() != (actual / path).read_bytes()
    )


def main() -> int:
    args = build_parser().parse_args()
    if args.check == (args.ref is not None):
        raise SystemExit("give either a ref or --check")

    if args.check:
        ref, commit = recorded_source()
        with tempfile.TemporaryDirectory() as tmp:
            rebuilt = Path(tmp) / "_vendor"
            build(ref, rebuilt, commit=commit)
            differences = tree_differences(rebuilt, VENDOR_DIR)
        if differences:
            print(f"src/conformance/_vendor/ differs from a rebuild of {ref} ({commit}):")
            for path in differences:
                print(f"  {path}")
            print("Change the bundled copy only through patches/aiosendspin/ and this script.")
            return 1
        print(f"src/conformance/_vendor/ matches {ref} ({commit})")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        rebuilt = Path(tmp) / "_vendor"
        build(args.ref, rebuilt)
        shutil.rmtree(VENDOR_DIR, ignore_errors=True)
        shutil.copytree(rebuilt, VENDOR_DIR)
    ref, commit = recorded_source()
    print(f"Vendored aiosendspin {ref} ({commit}) into src/conformance/_vendor/")
    return 0


def _export(ref: str, checkout: Path) -> str:
    """Check out upstream `ref` into `checkout` and return its commit."""
    _git(checkout, "init", "--quiet")
    _git(checkout, "fetch", "--quiet", "--depth", "1", UPSTREAM_URL, ref)
    _git(checkout, "-c", "advice.detachedHead=false", "checkout", "--quiet", "FETCH_HEAD")
    return _git(checkout, "rev-parse", "FETCH_HEAD^{commit}").strip()


def _apply_patch(patch: Path, checkout: Path) -> None:
    try:
        _git(checkout, "apply", str(patch))
    except SystemExit as err:
        raise SystemExit(f"patch {patch.name} does not apply:\n{err}") from err


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def _normalise_requirement(requirement: str) -> str:
    return "".join(requirement.split()).lower()


def _vendored_md(ref: str, commit: str, patches: list[Path]) -> str:
    patch_lines = [f"  - `{patch.name}`" for patch in patches] or ["  - none"]
    return "\n".join(
        [
            "# Vendored aiosendspin",
            "",
            "Generated by `scripts/vendor_aiosendspin.py`. Do not edit anything under",
            "`src/conformance/_vendor/` by hand: every change goes through a patch in",
            "`patches/aiosendspin/`, and CI fails when this tree differs from a rebuild.",
            "",
            f"- Upstream: {UPSTREAM_URL}",
            f"- Ref: `{ref}`",
            f"- Commit: `{commit}`",
            "- Patches, applied in order:",
            *patch_lines,
            "",
        ]
    )


def _files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


if __name__ == "__main__":
    raise SystemExit(main())
