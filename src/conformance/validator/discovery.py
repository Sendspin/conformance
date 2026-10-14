"""Finds Sendspin clients that advertise themselves over mDNS for server-initiated mode."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

CLIENT_SERVICE = "_sendspin._tcp.local."
_RESOLVE_TIMEOUT_MS = 3000


@dataclass(frozen=True, slots=True)
class DiscoveredClient:
    """A client advertising `_sendspin._tcp`; `name` is its TXT `name`, if it has one."""

    service: str
    url: str
    name: str | None

    @property
    def label(self) -> str:
        """How to show the client in a list."""
        return f"{self.name!r} {self.url}" if self.name is not None else self.url


@dataclass(frozen=True, slots=True)
class Discovery:
    """
    What a browse found.

    `clients` resolved to a WebSocket URL, sorted by URL. `unusable` describes each
    service that did not, and why, so a client advertising itself wrongly is shown
    rather than missed.
    """

    clients: list[DiscoveredClient]
    unusable: list[str]


async def discover_clients(seconds: float) -> Discovery:
    """Browse for `_sendspin._tcp` services for `seconds` and resolve each one found."""
    found: set[str] = set()

    def on_change(
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change is not ServiceStateChange.Removed:
            found.add(name)

    async_zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
    browser = AsyncServiceBrowser(async_zeroconf.zeroconf, CLIENT_SERVICE, handlers=[on_change])
    try:
        await asyncio.sleep(seconds)
        resolved = await asyncio.gather(
            *(_resolve(async_zeroconf.zeroconf, service) for service in sorted(found))
        )
    finally:
        await browser.async_cancel()
        await async_zeroconf.async_close()
    return Discovery(
        clients=sorted(
            (r for r in resolved if isinstance(r, DiscoveredClient)), key=lambda c: c.url
        ),
        unusable=[r for r in resolved if isinstance(r, str)],
    )


async def _resolve(zeroconf: Zeroconf, service: str) -> DiscoveredClient | str:
    """Return the client `service` advertises, or why it cannot be connected to."""
    info = AsyncServiceInfo(CLIENT_SERVICE, service)
    if not await info.async_request(zeroconf, _RESOLVE_TIMEOUT_MS) or info.port is None:
        return f"{service}: did not resolve to an address and port"
    addresses = info.parsed_addresses(IPVersion.V4Only)
    if not addresses:
        return f"{service}: has no IPv4 address"
    path = info.decoded_properties.get("path")
    if path is None or not path.startswith("/"):
        # connection.md:14 makes the TXT `path` key REQUIRED.
        return f"{service}: TXT path is {path!r}, not a path starting with /"
    return DiscoveredClient(
        service=service,
        url=f"ws://{addresses[0]}:{info.port}{path}",
        name=info.decoded_properties.get("name"),
    )
