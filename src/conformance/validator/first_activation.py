"""
Hold back aiosendspin's first `server/activate` on a dial, so a step can send its own.

A client admits a new server-initiated connection on its first `server/activate`
(connection.md, "Multiple servers (server-initiated)"). aiosendspin always opens with
its own activation, which on an unpaired connection declares no activities: the lowest
priority, which loses to any other server holding the client idle. A step that pairs,
or that sends an activation of its own, needs that activation to be the first one.

aiosendspin has no hook for this, so the validator wraps `SendspinConnection._activate`.
It reaches into private state the bundled aiosendspin pins, and tests cover that state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from conformance._vendor.aiosendspin.models.core import (
    ServerActivateMessage,
    ServerActivatePayload,
)
from conformance._vendor.aiosendspin.server.connection import SendspinConnection

_HELD_URLS: dict[str, Callable[[], None]] = {}
_HELD_ATTR = "_validator_held_activation"
_original_activate: Callable[[SendspinConnection], Awaitable[None]] | None = None


def install() -> None:
    """Wrap `SendspinConnection._activate`; calling it again does nothing."""
    global _original_activate  # noqa: PLW0603 - one wrapper for the process
    if _original_activate is not None:
        return
    _original_activate = SendspinConnection._activate  # noqa: SLF001
    SendspinConnection._activate = _activate  # type: ignore[method-assign] # noqa: SLF001


def hold_next(url: str, notify: Callable[[], None]) -> None:
    """Hold back the first activation of the next dial to `url`, calling `notify` once held."""
    _HELD_URLS[url] = notify


def forget(url: str) -> None:
    """Drop a hold no connection to `url` took up."""
    _HELD_URLS.pop(url, None)


def is_held(connection: SendspinConnection) -> bool:
    """Whether `connection` is still waiting for its first activation."""
    return bool(getattr(connection, _HELD_ATTR, False))


async def release(
    connection: SendspinConnection, payload: ServerActivatePayload | None = None
) -> None:
    """
    Send a held connection's first activation: `payload`, or aiosendspin's own.

    `payload` goes out as is, without activating any role inside aiosendspin, the way
    a step sends an activation aiosendspin would not send itself.
    """
    assert _original_activate is not None
    setattr(connection, _HELD_ATTR, False)
    if payload is None:
        await _original_activate(connection)
        return
    transport = connection._transport  # noqa: SLF001
    assert transport is not None
    connection._declared_activities = list(payload.activities)  # noqa: SLF001
    connection.send_priority_message(ServerActivateMessage(payload=payload))
    while await connection._process_priority_messages(transport):  # noqa: SLF001
        pass
    connection._resume_writer()  # noqa: SLF001


async def _activate(self: SendspinConnection) -> None:
    assert _original_activate is not None
    url = self._url  # noqa: SLF001
    first = self._declared_activities is None  # noqa: SLF001
    notify = _HELD_URLS.pop(url, None) if first and url is not None else None
    if notify is not None:
        setattr(self, _HELD_ATTR, True)
        # The writer starts only after the hellos; stopped, it sends nothing before the
        # step's activation. A pairing attempt, or `release`, starts it again.
        self._writer_paused = True  # noqa: SLF001
        self._writer_stopping = True  # noqa: SLF001
        notify()
        return
    setattr(self, _HELD_ATTR, False)
    await _original_activate(self)
