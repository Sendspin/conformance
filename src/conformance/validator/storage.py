"""The validator's persistent Noise identity and pairing records."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from conformance._vendor.aiosendspin.noise import (
    FileServerPairingStore,
    Identity,
    TrustedUnpairedClient,
    b64url_decode,
)

APP_NAME = "sendspin-validate"
_IDENTITY_FILE = "identity.json"
_PAIRING_FILE = "pairing.json"


class AdmitUnpairedStore(FileServerPairingStore):
    """
    A pairing store that approves every client for unpaired playback.

    The person running the validator is the operator of whatever client they test, so
    the validator admits it through unpaired access without storing an approval.
    """

    async def trusted_unpaired(self, client_id: str) -> TrustedUnpairedClient | None:
        """Return an approval for `client_id`."""
        return await super().trusted_unpaired(client_id) or TrustedUnpairedClient(client_id)


def user_data_dir() -> Path:
    """Return the platform's per-user data directory for the validator."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP_NAME


async def load_state(directory: Path, *, fresh: bool) -> tuple[Identity, AdmitUnpairedStore]:
    """
    Return the validator's identity and pairing store kept in `directory`.

    The first run creates both. `fresh` replaces the identity with a new one and
    discards every pairing record, since records made under the old identity no
    longer match it.
    """
    identity = await asyncio.to_thread(_load_identity, directory, fresh=fresh)
    return identity, await AdmitUnpairedStore.open(directory / _PAIRING_FILE)


def _load_identity(directory: Path, *, fresh: bool) -> Identity:
    directory.mkdir(parents=True, exist_ok=True)
    identity_path = directory / _IDENTITY_FILE
    if not fresh and identity_path.exists():
        stored = json.loads(identity_path.read_text(encoding="utf-8"))
        return Identity.from_private_bytes(b64url_decode(stored["private_key"]))
    identity = Identity.generate()
    _write_private(identity_path, json.dumps({"private_key": identity.private_b64u}))
    (directory / _PAIRING_FILE).unlink(missing_ok=True)
    return identity


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path`, readable by the current user only."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(text + "\n")
