"""Toolchain discovery helpers."""

from __future__ import annotations

import os
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


def find_java() -> str | None:
    """Return a usable java executable path."""
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / "java"
        if candidate.exists():
            return str(candidate)
    return shutil.which("java")
