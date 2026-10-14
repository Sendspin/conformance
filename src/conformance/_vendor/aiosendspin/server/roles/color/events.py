"""Color group role events."""

from __future__ import annotations

from dataclasses import dataclass

from conformance._vendor.aiosendspin.server.events import GroupRoleEvent
from conformance._vendor.aiosendspin.server.roles.color.state import Color


class ColorEvent(GroupRoleEvent):
    """Base event type for color group role changes."""


@dataclass
class ColorUpdatedEvent(ColorEvent):
    """Color palette was set or updated for the group."""

    color: Color
    previous_color: Color | None
    timestamp_us: int


@dataclass
class ColorClearedEvent(ColorEvent):
    """Color palette was cleared for the group."""

    previous_color: Color | None
    timestamp_us: int
