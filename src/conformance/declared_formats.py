"""Client-declared audio formats and which of them the matrix actually exercised.

Clients advertise the audio formats they can decode in `client/hello`, and the
harness records that hello verbatim in every server summary. This module joins
those declarations to the formats the matrix really negotiated, so an untested
claim is visible as such instead of hiding behind a green case.

The join is mostly observational: it reports what a client claimed and what the
wire carried. Two verdicts are drawn from it. `undeclared_format_violation`
asserts the spec MUST that a negotiated format be one the client listed, and
applies to every case. `format_priority_violation` asserts that the server
picked the first entry of a list offering it a choice, and applies only to a
scenario built around that choice. Whether any other scenario got the format it
set out to test is not recorded in machine-readable form anywhere yet.
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

# The codecs every server supports, one of which every player has to list.
MANDATORY_CODECS = ("flac", "pcm")

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


def _comparable_key(audio_format: dict[str, Any]) -> FormatKey:
    """Return the identity used to match a negotiated format against a declared one.

    `bit_depth` is ignored for `opus`, per the spec, so it is dropped from the
    key for that codec rather than compared.
    """
    if audio_format.get("codec") == "opus":
        return tuple(
            audio_format[field] for field in AUDIO_FORMAT_FIELDS if field != "bit_depth"
        )
    return format_key(audio_format)


def undeclared_format_violation(
    server_summary: dict[str, Any],
    client_summary: dict[str, Any],
) -> str | None:
    """Return why the case's negotiated formats break the client's declaration, or None.

    Both `stream/start` and the `client/state` player `format` carry the same
    spec MUST: the format has to be one the client listed in its
    `supported_formats`. The reason names every undeclared format, the summary
    field that observed it, and what the client actually offered — the three
    things a reader of the red cell needs to tell a server defect from a client
    misreport.

    Formats match on `AUDIO_FORMAT_FIELDS`, except that `bit_depth` is dropped
    for `opus`, which the spec says to ignore for that codec in both
    `supported_formats` and `stream/start`. Comparing it there would fail a
    conformant server for a field it was told not to honour, and a check that
    only earns its red cells by naming real violations cannot afford that. The
    report's declared-versus-exercised join keeps the full identity: it is
    descriptive, so distinguishing two opus entries that differ only in an
    ignored field costs nothing, while this verdict is normative and has to
    match the spec exactly.

    None means there is nothing to report, which deliberately covers two cases:
    no violation, and no declaration this harness could read. An unreadable
    claim is not evidence against an implementation, and `declared-formats.json`
    already publishes such claims per implementation, so skipping here hides
    nothing. One unparsable entry suppresses the check for the whole case,
    because a format that looks undeclared could be the very entry that failed
    to parse. An explicitly empty `supported_formats` is likewise read as no
    declaration; no implementation emits one today.
    """
    declared, unreadable = _declared_formats(server_summary)
    if not declared or unreadable:
        return None

    declared_keys = {_comparable_key(audio_format) for audio_format in declared}
    undeclared = [
        audio_format
        for audio_format in _negotiated_formats(server_summary, client_summary)
        if _comparable_key(audio_format) not in declared_keys
    ]
    if not undeclared:
        return None

    observed = ", ".join(
        f"{format_label(audio_format)} (via {', '.join(audio_format['sources'])})"
        for audio_format in undeclared
    )
    offered = ", ".join(format_label(audio_format) for audio_format in declared)
    return f"Negotiated format not declared by the client: {observed}; declared: {offered}"


def format_priority_violation(
    server_summary: dict[str, Any],
    *,
    preferred_codec: str,
) -> str | None:
    """Return why the case did not show the server honouring the client's priority, or None.

    For a scenario whose client lists `preferred_codec` first with a pcm or flac
    entry behind it. Two things are judged:

    - The stimulus. The declared list has to lead with `preferred_codec` and
      carry a pcm or flac entry, which the spec requires of every player. A list
      of one has no priority for the server to honour, and a list without the
      mandatory entry is a `client/hello` no conformant client sends.
    - The selection. The spec says a server SHOULD pick the highest-priority
      entry it can produce, and only servers that can produce `preferred_codec`
      reach such a scenario, so a server that streamed one of the lower entries
      did not honour the priority. The spec ranks a `format` the player prefers
      in `client/state` above the list order; no client adapter sets one in a
      scenario that applies this rule, so the list order is what decides.

    Unlike `undeclared_format_violation`, a declaration this harness cannot read
    is reported rather than skipped: the declared list is the stimulus here, so
    without it the case has not been shown to test anything. A missing server
    stream, or one in a codec the client never listed, is left to the checks
    that already name those.
    """
    declared, unreadable = _declared_formats(server_summary)
    if unreadable:
        return (
            "Client's supported_formats carries an entry that cannot be read as an audio "
            "format, so the declared priority cannot be checked"
        )
    if not declared:
        return (
            "The server summary records no supported_formats from client/hello, so the "
            "declared priority cannot be checked"
        )

    codec_label = preferred_codec.upper()
    offered = ", ".join(format_label(audio_format) for audio_format in declared)
    codecs = [audio_format["codec"] for audio_format in declared]
    if codecs[0] != preferred_codec:
        return (
            f"Client did not list {codec_label} first, so the case cannot test its "
            f"priority; declared: {offered}"
        )
    if not any(codec in MANDATORY_CODECS for codec in codecs):
        return (
            "Client listed no flac or pcm entry, which roles/player/v1.md requires of "
            f"every player; declared: {offered}"
        )

    stream = normalized_format(server_summary.get("stream"))
    if stream is not None and stream["codec"] != preferred_codec and stream["codec"] in codecs:
        return (
            f"Server selected {format_label(stream)} although the client listed "
            f"{codec_label} first; a server that can produce {codec_label} is expected to "
            "honour supported_formats priority (roles/player/v1.md, SHOULD); "
            f"declared: {offered}"
        )
    return None


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

    The server records the format it emitted in `stream/start`. Format-preference
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
