"""Toolchain discovery helpers."""

from __future__ import annotations

import shutil
from pathlib import Path


def find_dotnet() -> str | None:
    """Return a usable dotnet executable path."""
    dotnet = shutil.which("dotnet")
    if dotnet:
        return dotnet

    fallback = Path.home() / ".dotnet" / "dotnet"
    if fallback.exists():
        return str(fallback)
    return None


def find_cargo() -> str | None:
    """Return a usable cargo executable path."""
    return shutil.which("cargo")


def find_swift() -> str | None:
    """Return a usable swift executable path."""
    return shutil.which("swift")


def find_go() -> str | None:
    """Return a usable go executable path."""
    return shutil.which("go")


def find_cmake() -> str | None:
    """Return a usable cmake executable path."""
    return shutil.which("cmake")
