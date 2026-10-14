"""Async subprocess helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path


async def wait_for_file(path: Path, timeout_s: float) -> None:
    """Wait until a file appears on disk."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for file: {path}")


async def wait_for_exit(
    process: asyncio.subprocess.Process,
    *,
    role: str,
    timeout_s: float,
) -> None:
    """Wait until an adapter process exits, raising a TimeoutError that names the role."""
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_s)
    except TimeoutError:
        raise TimeoutError(
            f"Timed out after {timeout_s:g}s waiting for the {role} adapter to exit"
        ) from None


async def collect_process(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> asyncio.subprocess.Process:
    """Spawn a subprocess and tee stdout/stderr to a file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=env,
        stdout=handle,
        stderr=asyncio.subprocess.STDOUT,
    )
    setattr(process, "_conformance_log_handle", handle)
    return process


async def close_process_log(process: asyncio.subprocess.Process) -> None:
    """Close an attached log file handle if present."""
    handle = getattr(process, "_conformance_log_handle", None)
    if handle is not None:
        handle.close()
