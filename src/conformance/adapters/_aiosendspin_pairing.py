"""Shared Noise identity/pairing helpers for the aiosendspin server and client adapters.

The aiosendspin SDK requires every connection to be Noise-encrypted per the
spec's handshake (``messaging.md#communication``): each side needs a
long-term ``Identity`` keypair, and a long-term pre-shared key (PSK) makes
the connection admit as a paired ("trust_level": "user") session instead of
the unpaired sentinel-PSK fallback.

Conformance cases run the server and client adapters as two independent
processes with no side-channel beyond the CLI args the harness already
passes (``server_id``/``client_id``). Rather than inventing a new IPC
handshake to exchange keys/PSKs out of band, both processes deterministically
derive the *same* identity and long-term PSK from those already-shared string
ids. This is intentionally not cryptographically meaningful (the "secret" is
derivable by anyone who knows the ids) — it only needs to make both sides
agree on a pre-paired long-term credential so the case can exercise the real
Noise handshake and role-activation path the spec requires.
"""

from __future__ import annotations

import hashlib
from typing import Any


def _seeded_private_bytes(seed: str) -> bytes:
    return hashlib.sha256(f"sendspin-conformance-identity:{seed}".encode()).digest()


def deterministic_identity(seed: str) -> Any:
    """Return a stable ``Identity`` derived from ``seed`` (e.g. a role id string)."""
    from aiosendspin.noise.keys import Identity

    return Identity.from_private_bytes(_seeded_private_bytes(seed))


def deterministic_psk(server_id: str, client_id: str) -> bytes:
    """Return a stable 32-byte long-term PSK shared by ``server_id``/``client_id``."""
    return hashlib.sha256(f"sendspin-conformance-psk:{server_id}:{client_id}".encode()).digest()


async def make_server_identity_and_store(*, server_id: str, client_id: str) -> tuple[Any, Any]:
    """Return ``(identity, pairing_store)`` for the server, pre-paired with the client.

    The returned store already contains a long-term record admitting the
    conformance client's deterministic identity, so the very first connection
    can use the real (non-sentinel) long-term PSK path and receive the full
    spec handshake (``server/hello`` -> ``client/hello`` -> ``server/activate``).
    """
    from aiosendspin.noise.keys import psk_id_for
    from aiosendspin.noise.trust_store import InMemoryServerPairingStore, ServerPairingRecord

    identity = deterministic_identity(f"server:{server_id}")
    store = InMemoryServerPairingStore()
    psk = deterministic_psk(server_id, client_id)
    client_identity = deterministic_identity(f"client:{client_id}")
    await store.store_record(
        ServerPairingRecord(
            psk_id=psk_id_for(psk),
            psk=psk,
            client_id=client_identity.peer_id,
            pair_methods=[],
        )
    )
    return identity, store


async def make_client_identity_and_store(*, server_id: str, client_id: str) -> tuple[Any, Any]:
    """Return ``(identity, pairing_store)`` for the client, pre-paired with the server."""
    from aiosendspin.noise.keys import psk_id_for
    from aiosendspin.noise.trust_store import ClientPairingRecord, InMemoryClientPairingStore

    identity = deterministic_identity(f"client:{client_id}")
    store = InMemoryClientPairingStore()
    psk = deterministic_psk(server_id, client_id)
    server_identity = deterministic_identity(f"server:{server_id}")
    await store.store_record(
        ClientPairingRecord(
            psk_id=psk_id_for(psk),
            psk=psk,
            server_id=server_identity.peer_id,
        )
    )
    return identity, store
