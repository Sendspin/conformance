"""
Sendspin Server implementation to connect to and manage Sendspin Clients.

SendspinServer is the core of the music listening experience, responsible for:
- Managing connected clients
- Orchestrating synchronized grouped playback
"""

__all__ = [
    "AudioCodec",
    "AudioFormat",
    "ClientAddedEvent",
    "ClientConnectedEvent",
    "ClientCredentialMismatchEvent",
    "ClientDisconnectedEvent",
    "ClientEvent",
    "ClientGroupChangedEvent",
    "ClientRemovedEvent",
    "ClientRoleEvent",
    "ClientUpdatedEvent",
    "ConnectionSecurity",
    "DisconnectBehaviour",
    "ExternalStreamStartCallback",
    "ExternalStreamStartRequest",
    "FrameDirection",
    "FrameObserver",
    "FramePhase",
    "GroupDeletedEvent",
    "GroupEvent",
    "GroupMemberAddedEvent",
    "GroupMemberRemovedEvent",
    "GroupRoleEvent",
    "GroupStateChangedEvent",
    "MinBufferChangedEvent",
    "NoncomplianceObserver",
    "ObservedFrame",
    "ObservedNoncompliance",
    "OutputDelayChangedEvent",
    "RequiredLeadTimeChangedEvent",
    "SendspinClient",
    "SendspinEvent",
    "SendspinGroup",
    "SendspinServer",
    "SignalState",
    "SourceSignalChangedEvent",
    "SourceStream",
    "SourceStreamEndedEvent",
    "SourceStreamStartedEvent",
    "VolumeChangedEvent",
]

from conformance._vendor.aiosendspin.models.types import AudioCodec, SignalState
from conformance._vendor.aiosendspin.noise.wire import FrameDirection, FramePhase

from .audio import AudioFormat
from .client import ConnectionSecurity, DisconnectBehaviour, SendspinClient
from .connection import FrameObserver, NoncomplianceObserver, ObservedFrame, ObservedNoncompliance
from .events import (
    ClientEvent,
    ClientGroupChangedEvent,
    ClientRoleEvent,
    GroupDeletedEvent,
    GroupEvent,
    GroupMemberAddedEvent,
    GroupMemberRemovedEvent,
    GroupRoleEvent,
    GroupStateChangedEvent,
)
from .group import (
    SendspinGroup,
)
from .roles.player.events import (
    MinBufferChangedEvent,
    OutputDelayChangedEvent,
    RequiredLeadTimeChangedEvent,
    VolumeChangedEvent,
)
from .roles.source import (
    SourceSignalChangedEvent,
    SourceStream,
    SourceStreamEndedEvent,
    SourceStreamStartedEvent,
)
from .server import (
    ClientAddedEvent,
    ClientConnectedEvent,
    ClientCredentialMismatchEvent,
    ClientDisconnectedEvent,
    ClientRemovedEvent,
    ClientUpdatedEvent,
    ExternalStreamStartCallback,
    ExternalStreamStartRequest,
    SendspinEvent,
    SendspinServer,
)
