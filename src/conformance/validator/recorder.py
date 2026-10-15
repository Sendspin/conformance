"""
Records every frame the validator's server exchanges, decoded and redacted.

Redaction happens here, as each frame is recorded, so no secret reaches memory that
outlives the frame, `--verbose` output, or the protocol log.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

from conformance._vendor.aiosendspin.server import (
    FramePhase,
    ObservedFrame,
    ObservedNoncompliance,
)

from .prompter import printable

REDACTED = "<redacted>"
# A redacted secret field keeps its length, which the spec fixes and checks judge.
_REDACTED_FIELD = re.compile(r"<redacted:(\d+)>")
# Pairing secrets the spec carries on the wire, by field name.
_SECRET_FIELDS = frozenset({"long_term_psk", "wrapped_psk", "wrapped_nonce_B"})
# Transport type byte of a JSON message.
_JSON_MESSAGE = 0


@dataclass(frozen=True, slots=True)
class RecordedFrame:
    """
    One frame as the validator recorded it.

    `message` is the decoded JSON message, redacted, or None for a binary frame or one
    that did not decode. Binary content is never kept, only its type byte and size.
    `error` says why a frame did not decode, whether aiosendspin or the recorder found it.
    """

    id: int
    connection: int
    client_id: str | None
    direction: str
    phase: str
    monotonic_us: int
    size: int
    message: dict[str, Any] | None = None
    binary_type: int | None = None
    error: str | None = None

    @property
    def inbound(self) -> bool:
        """Whether the client sent this frame."""
        return self.direction == "in"

    @property
    def type(self) -> str | None:
        """The message `type`, or None when the frame carries no JSON message with one."""
        if self.message is None:
            return None
        message_type = self.message.get("type")
        return message_type if isinstance(message_type, str) else None

    @property
    def payload(self) -> dict[str, Any] | None:
        """The message `payload` object, or None when it has none."""
        if self.message is None:
            return None
        payload = self.message.get("payload")
        return payload if isinstance(payload, dict) else None

    def to_json(self) -> dict[str, Any]:
        """Return the frame as one `protocol.log.jsonl` line records it."""
        record: dict[str, Any] = {
            "client_id": self.client_id,
            "connection": self.connection,
            "direction": self.direction,
            "id": self.id,
            "monotonic_us": self.monotonic_us,
            "phase": self.phase,
            "size": self.size,
        }
        if self.message is not None:
            record["message"] = self.message
        if self.binary_type is not None:
            record["binary_type"] = self.binary_type
        if self.error is not None:
            record["error"] = self.error
        return record


@dataclass(frozen=True, slots=True)
class RecordedNoncompliance:
    """
    A spec violation the server flagged, which a strict server rejects the client for.

    `after_frame` is the id of the last frame recorded on the connection before it,
    the frame most likely to have caused it.
    """

    connection: int | None
    client_id: str | None
    reason: str
    monotonic_us: int
    after_frame: int | None


class EventKind(StrEnum):
    """Something that happened to a connection that no frame shows."""

    KEYED = "keyed"
    """A handshake completed; the event's `psk_category` names the PSK keying the session."""
    CREDENTIAL_MISMATCH = "credential_mismatch"
    """The client answered under the Sentinel PSK, unable to use the long-term PSK referenced."""
    CLOSED = "closed"
    """The connection ended."""
    PAIRING_FAILED = "pairing_failed"
    """A pairing attempt ended without pairing; the event's `detail` says why."""


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    """
    A connection event the server reported, which the encrypted frames cannot show.

    `psk_category` is aiosendspin's `PskCategory` value for a KEYED event, and None
    otherwise. `detail` is aiosendspin's reason for a PAIRING_FAILED event, redacted.
    `after_frame` is the id of the last frame recorded on the connection before it.
    """

    connection: int
    client_id: str
    kind: EventKind
    after_frame: int | None
    psk_category: str | None = None
    detail: str | None = None


class Recorder:
    """
    Observer for the validator's `SendspinServer` that keeps every frame and violation.

    Pass `observe_frame` as its `frame_observer` and `observe_noncompliance` as its
    `noncompliance_observer`; the session reports connection events to `observe_event`.
    A frame the recorder itself fails to process is kept in
    `tool_errors` rather than raised: aiosendspin would swallow the exception, and the
    validator must report what it could not observe.
    """

    def __init__(self, *, verbose: TextIO | None = None) -> None:
        self.frames: list[RecordedFrame] = []
        self.noncompliance: list[RecordedNoncompliance] = []
        self.events: list[RecordedEvent] = []
        self.tool_errors: list[str] = []
        self._secrets: set[str] = set()
        self._verbose = verbose
        self._changed = asyncio.Event()

    def add_secret(self, secret: str) -> None:
        """Redact `secret`, such as a pairing token or an entered code, wherever it appears."""
        if secret:
            self._secrets.add(secret)

    def notify(self) -> None:
        """Wake every `wait_until`, for a change the recorder does not see, like a disconnect."""
        self._changed.set()

    async def wait_until(self, condition: Callable[[], bool]) -> None:
        """Return once `condition` holds, testing it again after every recorded event."""
        while not condition():
            self._changed.clear()
            await self._changed.wait()

    def observe_frame(self, frame: ObservedFrame) -> None:
        """Record one frame the server sent or received."""
        self._changed.set()
        try:
            recorded = self._decode(frame)
        except Exception as exc:  # noqa: BLE001 - aiosendspin would swallow it unreported
            self.tool_errors.append(
                f"could not record frame {len(self.frames) + 1} on connection "
                f"{frame.connection}: {type(exc).__name__}: {exc}"
            )
            return
        self.frames.append(recorded)
        if self._verbose is not None:
            print(_describe(recorded), file=self._verbose, flush=True)

    def observe_noncompliance(self, observation: ObservedNoncompliance) -> None:
        """Record one spec violation the server flagged."""
        self._changed.set()
        recorded = RecordedNoncompliance(
            connection=observation.connection,
            client_id=observation.client_id,
            reason=self._redact_text(observation.reason),
            monotonic_us=observation.monotonic_us,
            after_frame=self._last_frame(observation.connection),
        )
        self.noncompliance.append(recorded)
        if self._verbose is not None:
            print(
                printable(f"! c{recorded.connection} {recorded.reason}"),
                file=self._verbose,
                flush=True,
            )

    def observe_event(
        self,
        connection: int,
        client_id: str,
        kind: EventKind,
        *,
        psk_category: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Record one connection event the server reported."""
        self._changed.set()
        event = RecordedEvent(
            connection=connection,
            client_id=client_id,
            kind=kind,
            after_frame=self._last_frame(connection),
            psk_category=psk_category,
            detail=self._redact_text(detail) if detail is not None else None,
        )
        self.events.append(event)
        if self._verbose is not None:
            extra = "".join(f" {v}" for v in (event.psk_category, event.detail) if v is not None)
            print(printable(f"~ c{connection} {kind}{extra}"), file=self._verbose, flush=True)

    def write_log(self, path: Path) -> None:
        """Write every recorded frame to `path` as JSON lines, in recording order."""
        with path.open("w", encoding="utf-8") as log:
            for frame in self.frames:
                log.write(json.dumps(frame.to_json(), sort_keys=True) + "\n")

    def _last_frame(self, connection: int | None) -> int | None:
        return next((f.id for f in reversed(self.frames) if f.connection == connection), None)

    def _decode(self, frame: ObservedFrame) -> RecordedFrame:
        common = {
            "id": len(self.frames) + 1,
            "connection": frame.connection,
            "client_id": frame.client_id,
            "direction": frame.direction.value,
            "phase": frame.phase.value,
            "monotonic_us": frame.monotonic_us,
            "size": len(frame.data),
        }
        if frame.error is not None:
            return RecordedFrame(**common, error=self._redact_text(frame.error))
        if frame.phase is FramePhase.CLEARTEXT:
            text = frame.data
        elif frame.data and frame.data[0] == _JSON_MESSAGE:
            text = frame.data[1:]
        else:
            binary_type = frame.data[0] if frame.data else None
            return RecordedFrame(**common, binary_type=binary_type)
        try:
            message = json.loads(text.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return RecordedFrame(**common, error=f"not a JSON message: {exc}")
        if not isinstance(message, dict):
            return RecordedFrame(**common, error="not a JSON object")
        return RecordedFrame(**common, message=self._redact(message))

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: _redact_field(item) if _is_secret_field(key) else self._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            return self._redact_text(value)
        return value

    def _redact_text(self, text: str) -> str:
        # Longest first, so a secret containing another is redacted whole.
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return text


def redacted_length(value: Any) -> int | None:
    """Return the length of the string a secret field carried, or None if it held no string."""
    match = _REDACTED_FIELD.fullmatch(value) if isinstance(value, str) else None
    return int(match.group(1)) if match else None


def _is_secret_field(key: str) -> bool:
    return key in _SECRET_FIELDS or key.startswith("pake_msg_") or key.endswith("_kc")


def _redact_field(value: Any) -> str:
    """Return the redacted form of a secret field: its length if it is a string."""
    return f"<redacted:{len(value)}>" if isinstance(value, str) else REDACTED


def _describe(frame: RecordedFrame) -> str:
    """Return a one-line, terminal-safe description of `frame` for `--verbose`."""
    arrow = "<-" if frame.inbound else "->"
    head = f"#{frame.id} c{frame.connection} {arrow} {frame.phase}"
    if frame.error is not None:
        body = f"error: {frame.error}"
    elif frame.message is not None:
        body = json.dumps(frame.message, sort_keys=True)
    else:
        body = f"binary type {frame.binary_type} ({frame.size} bytes)"
    return printable(f"{head} {body}")
