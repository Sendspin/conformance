"""The validator's persistent Noise identity and pairing records."""

from __future__ import annotations

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

    The person running the validator is the operator of whatever client connects, so
    the validator admits it through unpaired access unless an approval is revoked.
    Approvals and revocations last for the run only; pairing records persist.
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        self._revoked: set[str] = set()

    async def trusted_unpaired(self, client_id: str) -> TrustedUnpairedClient | None:
        """Return an approval for `client_id` unless it was revoked."""
        if client_id in self._revoked:
            return None
        return await super().trusted_unpaired(client_id) or TrustedUnpairedClient(client_id)

    async def add_trusted_unpaired(self, client: TrustedUnpairedClient) -> None:
        """Approve `client` again after a revocation."""
        self._revoked.discard(client.client_id)
        await super().add_trusted_unpaired(client)

    async def remove_trusted_unpaired(self, client_id: str) -> None:
        """Revoke `client_id`'s unpaired-playback approval for the rest of the run."""
        self._revoked.add(client_id)
        await super().remove_trusted_unpaired(client_id)


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
    directory.mkdir(parents=True, exist_ok=True)
    identity_path = directory / _IDENTITY_FILE
    pairing_path = directory / _PAIRING_FILE
    if fresh or not identity_path.exists():
        identity = Identity.generate()
        _write_private(identity_path, json.dumps({"private_key": identity.private_b64u}))
        pairing_path.unlink(missing_ok=True)
    else:
        stored = json.loads(identity_path.read_text(encoding="utf-8"))
        identity = Identity.from_private_bytes(b64url_decode(stored["private_key"]))
    return identity, await AdmitUnpairedStore.open(pairing_path)


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path`, readable by the current user only."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(text + "\n")
