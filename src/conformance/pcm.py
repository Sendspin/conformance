"""PCM hashing helpers used across adapters."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Iterable


def sha256_hex(data: bytes) -> str:
    """Return a SHA-256 hex digest."""
    return hashlib.sha256(data).hexdigest()


def pcm_int_bytes_to_float_bytes(pcm_bytes: bytes, bit_depth: int) -> bytes:
    """Convert little-endian integer PCM bytes to canonical float32 bytes."""
    if bit_depth == 16:
        sample_count = len(pcm_bytes) // 2
        ints = struct.unpack(f"<{sample_count}h", pcm_bytes)
        floats = [sample / 32768.0 for sample in ints]
        return struct.pack(f"<{len(floats)}f", *floats)
    if bit_depth == 24:
        # struct.unpack rejects a partial trailing sample for 16- and 32-bit. The manual
        # 24-bit loop has to say so itself, or the same input surfaces as an opaque
        # IndexError from the middle of the unpack.
        if len(pcm_bytes) % 3:
            raise ValueError(
                f"PCM buffer of {len(pcm_bytes)} bytes is not a whole number of 24-bit samples"
            )
        floats: list[float] = []
        for offset in range(0, len(pcm_bytes), 3):
            value = pcm_bytes[offset] | (pcm_bytes[offset + 1] << 8) | (pcm_bytes[offset + 2] << 16)
            if value & 0x800000:
                value |= ~0xFFFFFF
            floats.append(value / 8388608.0)
        return struct.pack(f"<{len(floats)}f", *floats)
    if bit_depth == 32:
        sample_count = len(pcm_bytes) // 4
        ints = struct.unpack(f"<{sample_count}i", pcm_bytes)
        floats = [sample / 2147483648.0 for sample in ints]
        return struct.pack(f"<{len(floats)}f", *floats)
    raise ValueError(f"Unsupported PCM bit depth: {bit_depth}")


def float_values_to_bytes(values: Iterable[float]) -> bytes:
    """Convert float values to canonical little-endian float32 bytes."""
    materialized = list(values)
    return struct.pack(f"<{len(materialized)}f", *materialized)


@dataclass
class FloatPcmHasher:
    """Incremental canonical PCM hash helper."""

    sample_count: int = 0
    _hash: hashlib._Hash = field(default_factory=hashlib.sha256)

    def update_from_float_bytes(self, float_bytes: bytes) -> None:
        """Update the hash with canonical float32 bytes."""
        self._hash.update(float_bytes)
        self.sample_count += len(float_bytes) // 4

    def update_from_pcm_bytes(self, pcm_bytes: bytes, bit_depth: int) -> None:
        """Update the hash from integer PCM bytes."""
        self.update_from_float_bytes(pcm_int_bytes_to_float_bytes(pcm_bytes, bit_depth))

    def hexdigest(self) -> str:
        """Return the current digest."""
        return self._hash.hexdigest()
