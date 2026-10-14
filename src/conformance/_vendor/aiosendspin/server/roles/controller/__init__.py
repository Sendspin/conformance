"""Controller role - client and group level."""

from conformance._vendor.aiosendspin.server.roles.controller.events import (
    ControllerEvent,
    ControllerMuteEvent,
    ControllerNextEvent,
    ControllerPauseEvent,
    ControllerPlayEvent,
    ControllerPreviousEvent,
    ControllerRepeatEvent,
    ControllerSeekEvent,
    ControllerSeekRelativeEvent,
    ControllerShuffleEvent,
    ControllerStopEvent,
    ControllerSwitchEvent,
    ControllerVolumeEvent,
)
from conformance._vendor.aiosendspin.server.roles.controller.group import ControllerGroupRole
from conformance._vendor.aiosendspin.server.roles.controller.types import ControllerRoleProtocol
from conformance._vendor.aiosendspin.server.roles.controller.v1 import ControllerV1Role
from conformance._vendor.aiosendspin.server.roles.registry import register_group_role, register_role

register_group_role("controller", ControllerGroupRole)
register_role("controller@v1", lambda client: ControllerV1Role(client=client))

__all__ = [
    "ControllerEvent",
    "ControllerGroupRole",
    "ControllerMuteEvent",
    "ControllerNextEvent",
    "ControllerPauseEvent",
    "ControllerPlayEvent",
    "ControllerPreviousEvent",
    "ControllerRepeatEvent",
    "ControllerRoleProtocol",
    "ControllerSeekEvent",
    "ControllerSeekRelativeEvent",
    "ControllerShuffleEvent",
    "ControllerStopEvent",
    "ControllerSwitchEvent",
    "ControllerV1Role",
    "ControllerVolumeEvent",
]
