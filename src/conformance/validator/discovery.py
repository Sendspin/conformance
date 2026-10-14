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


async def discover_clients(seconds: float) -> list[DiscoveredClient]:
    """
    Browse for `seconds` and return every client that resolved to a WebSocket URL.

    A service whose TXT record lacks a `path` starting with `/` is left out, since the
    spec makes `path` required. The list is sorted by URL.
    """
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
        resolved = [await _resolve(async_zeroconf.zeroconf, service) for service in sorted(found)]
    finally:
        await browser.async_cancel()
        await async_zeroconf.async_close()
    return sorted((client for client in resolved if client is not None), key=lambda c: c.url)


async def _resolve(zeroconf: Zeroconf, service: str) -> DiscoveredClient | None:
    info = AsyncServiceInfo(CLIENT_SERVICE, service)
    if not await info.async_request(zeroconf, _RESOLVE_TIMEOUT_MS) or info.port is None:
        return None
    addresses = info.parsed_addresses(IPVersion.V4Only)
    path = info.decoded_properties.get("path")
    if not addresses or path is None or not path.startswith("/"):
        return None
    return DiscoveredClient(
        service=service,
        url=f"ws://{addresses[0]}:{info.port}{path}",
        name=info.decoded_properties.get("name"),
    )
