"""Client-declared audio formats and which of them the matrix actually exercised.

Clients advertise the audio formats they can decode in `client/hello`, and the
harness records that hello verbatim in every server summary. This module joins
those declarations to the formats the matrix really negotiated, so an untested
claim is visible as such instead of hiding behind a green case.

The join is observational. It reports what a client claimed and what the wire
carried; it says nothing about whether a scenario got the format it set out to
test, which is not recorded in machine-readable form anywhere yet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .implementations import implementation_names
from .io import read_json
from .models import AUDIO_FORMAT_FIELDS

SCHEMA_VERSION = 1

PLAYER_SUPPORT_KEY = "player@v1_support"

FormatKey = tuple[Any, ...]

_NUMERIC_FIELDS = tuple(field for field in AUDIO_FORMAT_FIELDS if field != "codec")


def normalized_format(value: Any) -> dict[str, Any] | None:
    """Return the audio format an adapter summary object describes, or None.

    Codecs are casefolded because they reach the summaries from independent
    producers: aiosendspin serializes an enum value, while the Go server copies
    the raw wire envelope through untouched.
    """
    if not isinstance(value, dict):
        return None
    codec = value.get("codec")
    if not isinstance(codec, str) or not codec:
        return None
    normalized: dict[str, Any] = {"codec": codec.casefold()}
    for field in _NUMERIC_FIELDS:
        number = value.get(field)
        # bool is an int subclass, and an artwork stream carries a list of
        # channel descriptors under "channels" rather than a count.
        if isinstance(number, bool) or not isinstance(number, int):
            return None
        normalized[field] = number
    return normalized


def format_key(audio_format: dict[str, Any]) -> FormatKey:
    """Return the hashable identity of a normalized audio format.

    Reads the field set rather than restating it, so widening
    ``AUDIO_FORMAT_FIELDS`` cannot leave two distinct formats sharing one key.
    """
    return tuple(audio_format[field] for field in AUDIO_FORMAT_FIELDS)


def format_label(audio_format: dict[str, Any]) -> str:
    """Describe a normalized audio format for a human reading the report."""
    channels = int(audio_format["channels"])
    channel_label = {1: "mono", 2: "stereo"}.get(channels, f"{channels} ch")
    rate_label = f"{int(audio_format['sample_rate']) / 1000:g} kHz"
    return (
        f"{str(audio_format['codec']).upper()} · {channel_label} · "
        f"{rate_label} · {int(audio_format['bit_depth'])}-bit"
    )


def _declared_formats(
    server_summary: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the formats the client advertised, plus any entry unreadable as one.

    A claim that cannot be parsed is reported rather than dropped: a declaration
    vanishing unnoticed is the failure mode this report exists to expose.
    """
    peer_hello = server_summary.get("peer_hello")
    if not isinstance(peer_hello, dict):
        return [], []
    payload = peer_hello.get("payload")
    if not isinstance(payload, dict):
        return [], []
    player_support = payload.get(PLAYER_SUPPORT_KEY)
    if not isinstance(player_support, dict):
        return [], []
    supported_formats = player_support.get("supported_formats")
    if not isinstance(supported_formats, list):
        if supported_formats is not None:
            return [], [{"value": supported_formats, "reason": "supported_formats is not a list"}]
        return [], []

    declared: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []
    seen: set[FormatKey] = set()
    for entry in supported_formats:
        audio_format = normalized_format(entry)
        if audio_format is None:
            unreadable.append(
                {
                    "value": entry,
                    "reason": (
                        "entry does not carry a codec with integer "
                        "sample_rate, bit_depth and channels"
                    ),
                }
            )
            continue
        key = format_key(audio_format)
        if key in seen:
            continue
        seen.add(key)
        declared.append(audio_format)
    return declared, unreadable


def _negotiated_formats(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return every audio format the case negotiated, tagged with its source.

    The server records the format it emitted in `stream/start`. Renegotiation
    scenarios emit two and only the client keeps both, so both sides are read
    and each observation names the summary field it came from.
    """
    candidates: list[tuple[str, Any]] = [
        ("server.stream", server_summary.get("stream")),
        ("client.stream", client_summary.get("stream")),
    ]
    renegotiation = client_summary.get("renegotiation")
    if isinstance(renegotiation, dict):
        candidates.append(
            ("client.renegotiation.initial_format", renegotiation.get("initial_format"))
        )
        candidates.append(
            ("client.renegotiation.final_format", renegotiation.get("final_format"))
        )

    negotiated: list[dict[str, Any]] = []
    sources: dict[FormatKey, list[str]] = {}
    for source, candidate in candidates:
        audio_format = normalized_format(candidate)
        if audio_format is None:
            continue
        key = format_key(audio_format)
        if key in sources:
            sources[key].append(source)
            continue
        sources[key] = [source]
        negotiated.append({**audio_format, "sources": sources[key]})
    return negotiated


def _read_summary(path: Path) -> dict[str, Any]:
    """Read an adapter summary, treating an absent or unparsable one as empty."""
    if not path.exists():
        return {}
    try:
        summary = read_json(path)
    except (OSError, ValueError):
        return {}
    return summary if isinstance(summary, dict) else {}


def _case_reference(case: dict[str, Any], *, sources: list[str] | None = None) -> dict[str, Any]:
    """Point at one case, optionally naming which summary fields observed it."""
    reference = {
        "case_name": case["case_name"],
        "scenario_id": case["scenario_id"],
        "server_impl": case["server_impl"],
        "status": case["status"],
    }
    if sources is not None:
        reference["sources"] = sources
    return reference


def _case_declarations(data_dir: Path, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for result in results:
        case_name = Path(str(result.get("case_dir") or "")).name
        if not case_name:
            continue
        case_dir = data_dir / case_name
        server_summary = _read_summary(case_dir / "server-summary.json")
        client_summary = _read_summary(case_dir / "client-summary.json")
        declared, unreadable = _declared_formats(server_summary)
        negotiated = _negotiated_formats(server_summary, client_summary)
        if not declared and not negotiated and not unreadable:
            continue
        cases.append(
            {
                "case_name": case_name,
                "scenario_id": str(result.get("scenario_id") or ""),
                "server_impl": str(result.get("server_impl") or ""),
                "client_impl": str(result.get("client_impl") or ""),
                "status": str(result.get("status") or ""),
                "declared": declared,
                "negotiated": negotiated,
                "unreadable_declarations": unreadable,
            }
        )
    cases.sort(key=lambda case: (case["client_impl"], case["scenario_id"], case["case_name"]))
    return cases


def _implementation_entry(client_impl: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    formats: dict[FormatKey, dict[str, Any]] = {}
    declared_by: dict[FormatKey, list[dict[str, Any]]] = {}
    exercised_by: dict[FormatKey, list[dict[str, Any]]] = {}

    for case in cases:
        for audio_format in case["declared"]:
            key = format_key(audio_format)
            formats.setdefault(key, {field: audio_format[field] for field in AUDIO_FORMAT_FIELDS})
            declared_by.setdefault(key, []).append(_case_reference(case))
        for audio_format in case["negotiated"]:
            # Which summary field saw the format travels with the observation:
            # a format only ever seen in the client's own summary is weaker
            # evidence than one the server recorded emitting.
            exercised_by.setdefault(format_key(audio_format), []).append(
                _case_reference(case, sources=list(audio_format.get("sources") or []))
            )

    entries: list[dict[str, Any]] = []
    for key in sorted(formats):
        exercising = exercised_by.get(key, [])
        entries.append(
            {
                **formats[key],
                "label": format_label(formats[key]),
                "exercised": bool(exercising),
                "declared_by": declared_by.get(key, []),
                "exercised_by": exercising,
            }
        )

    return {
        "implementation": client_impl,
        "declared_count": len(entries),
        "exercised_count": sum(1 for entry in entries if entry["exercised"]),
        "formats": entries,
        "unreadable_declarations": _rolled_up_unreadable(cases),
    }


def _rolled_up_unreadable(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group identical unreadable claims so one bad entry is not listed per case."""
    grouped: dict[str, dict[str, Any]] = {}
    for case in cases:
        for entry in case["unreadable_declarations"]:
            identity = json.dumps([entry["value"], entry["reason"]], sort_keys=True)
            group = grouped.get(identity)
            if group is None:
                grouped[identity] = {
                    "value": entry["value"],
                    "reason": entry["reason"],
                    "declared_by": [_case_reference(case)],
                }
                continue
            group["declared_by"].append(_case_reference(case))
    return [grouped[identity] for identity in sorted(grouped)]


def collect_declared_formats(
    data_dir: Path,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Join client format declarations to the formats the matrix negotiated.

    The per-implementation rollup is derived from the same per-case pass that is
    published, so the two views cannot drift apart.
    """
    cases = _case_declarations(data_dir, results)
    cases_by_client: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        cases_by_client.setdefault(case["client_impl"], []).append(case)

    known_names = implementation_names()
    ordered_clients = [name for name in known_names if name in cases_by_client]
    ordered_clients.extend(
        sorted(name for name in cases_by_client if name not in set(known_names))
    )

    implementations = [
        _implementation_entry(client_impl, cases_by_client[client_impl])
        for client_impl in ordered_clients
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "implementations": [
            entry
            for entry in implementations
            if entry["formats"] or entry["unreadable_declarations"]
        ],
        "cases": cases,
    }


def declared_formats_for_implementation(
    payload: dict[str, Any],
    implementation: str,
) -> dict[str, Any] | None:
    """Return one implementation's entry from a collected payload."""
    implementations = payload.get("implementations")
    if not isinstance(implementations, list):
        return None
    for entry in implementations:
        if isinstance(entry, dict) and entry.get("implementation") == implementation:
            return entry
    return None
