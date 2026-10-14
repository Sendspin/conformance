"""Metadata group role events."""

from __future__ import annotations

from dataclasses import dataclass

from conformance._vendor.aiosendspin.server.events import GroupRoleEvent
from conformance._vendor.aiosendspin.server.roles.metadata.state import Metadata


class MetadataEvent(GroupRoleEvent):
    """Base event type for metadata group role changes."""


@dataclass
class MetadataUpdatedEvent(MetadataEvent):
    """Metadata was set or updated for the group."""

    metadata: Metadata
    previous_metadata: Metadata | None
    timestamp_us: int


@dataclass
class MetadataClearedEvent(MetadataEvent):
    """Metadata was cleared for the group."""

    previous_metadata: Metadata | None
    timestamp_us: int
