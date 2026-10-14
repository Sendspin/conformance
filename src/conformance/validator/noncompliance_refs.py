"""
Spec references for the violations aiosendspin's strict mode rejects a client for.

Every reason the bundled aiosendspin can report is listed as a full-match pattern
with the lines of the Sendspin spec, at tag 1.0.0-rc1, holding the rule it enforces.
A reference of None marks a rule aiosendspin enforces that rc1 does not make.
"""

from __future__ import annotations

import re

_REFS: tuple[tuple[str, str | None], ...] = (
    (
        r"(.+) sent (a boolean|a number|a string) for '(.+)' instead of an integer",
        "messaging.md:24",
    ),
    (r"(.+) sent (a boolean|a number|a string) instead of an integer", "messaging.md:24"),
    (r"(.+) sent (a string|an integer) for '(.+)' instead of a boolean", "messaging.md:24"),
    (r"(.+) sent (a string|an integer) instead of a boolean", "messaging.md:24"),
    (r"(.+) sent (an integer|a number) for '(.+)' instead of a string", "messaging.md:24"),
    (r"(.+) sent (an integer|a number) instead of a string", "messaging.md:24"),
    (r"(.+) sent (an object) for '(.+)' instead of an array", "messaging.md:24"),
    (r"(.+) sent (an object) instead of an array", "messaging.md:24"),
    (r"(.+) sent (null) for '(.+)' instead of a value", "messaging.md:22"),
    (r"(.+) sent (null) instead of a value", "messaging.md:22"),
    (r"Noise message 2 data is not canonical base64url", "messaging.md:164"),
    (r"Noise message 2 payload is not the literal bytes \{\}", "messaging.md:171"),
    (r"Pairing PSK client/pair-finalize sent without client/pair-init", "pairing.md:59"),
    (
        r"client-stream/start announced codec '(opus|flac|pcm)', which server/hello did not list",
        "roles/source/v1.md:20",
    ),
    (r"client/hello artwork declared the removed 'bmp' format", "roles/artwork/v1.md:13"),
    (
        r"client/hello artwork used pre-rename dimension keys: (.+)",
        "messaging.md:28, roles/artwork/v1.md:14-15",
    ),
    (
        r"client/hello declared artwork@v1_support, superseded by the client/state artwork object",
        "messaging.md:28, roles/artwork/v1.md:10",
    ),
    (
        r"client/hello declared player supported_commands, superseded by client/state",
        "messaging.md:28, roles/player/v1.md:50",
    ),
    (
        r"client/hello declared visualizer stream configuration, superseded by client/state",
        "messaging.md:28, roles/visualizer/v1.md:19",
    ),
    (r"client/hello did not offer the pairing_psk method", "messaging.md:219, pairing.md:309"),
    (r"client/hello listed roles not of the form role@version", "messaging.md:208"),
    (
        r"client/hello listed roles without their required support object, not activating: (.+)",
        "messaging.md:216-218, messaging.md:225",
    ),
    (r"client/hello offered qr_code without a display out_channel", "pairing.md:315"),
    (
        r"client/hello offered the pre-rename PIN pairing methods",
        "messaging.md:28, pairing.md:311, pairing.md:313, pairing.md:319",
    ),
    (r"client/hello omitted the required supported_pair_methods", "messaging.md:219"),
    (r"client/hello omitted the required unpaired_access\.enabled", "messaging.md:220-221"),
    (r"client/hello player@v1_support listed neither flac nor pcm", "roles/player/v1.md:20"),
    (r"client/hello sent a mac_address not in lowercase colon-separated form", "messaging.md:207"),
    (r"client/hello sent a malformed pair-method descriptor", "pairing.md:309-315"),
    (r"client/hello sent support objects for unlisted roles: (.+)", "messaging.md:216-218"),
    (r"client/hello sent supported_pair_methods as a list", "messaging.md:219"),
    (r"client/hello sent the removed trust_level key", "messaging.md:28"),
    (r"client/hello used unversioned support keys: (.+)", "messaging.md:223"),
    (r"client/init client_id is not canonical base64url", "messaging.md:145"),
    (r"client/pair-retry omitted the required payload object", "pairing.md:375"),
    (r"client/pair-retry sent a payload that is not an object", "pairing.md:375"),
    (
        r"client/state after server/activate has an active artwork role but no artwork state",
        "messaging.md:297",
    ),
    (
        r"client/state after server/activate has an active player role but no player state",
        "messaging.md:297",
    ),
    (
        r"client/state after server/activate has an active source role but no source state",
        "messaging.md:297",
    ),
    (
        r"client/state after server/activate has an active visualizer role but no visualizer state",
        "messaging.md:297",
    ),
    (r"client/state artwork declared the removed 'bmp' format", "roles/artwork/v1.md:13"),
    (
        r"client/state artwork used pre-rename dimension keys: (.+)",
        "messaging.md:28, roles/artwork/v1.md:14-15",
    ),
    (r"client/state declared the pre-rename 'set_static_delay' command", "roles/player/v1.md:50"),
    (r"client/state declared unrecognized supported_commands", "roles/player/v1.md:50"),
    (r"client/state omitted muted despite declaring the mute command", "roles/player/v1.md:46"),
    (r"client/state omitted required player timing fields", "roles/player/v1.md:47-49"),
    (r"client/state omitted required supported_commands", "roles/player/v1.md:50"),
    (r"client/state omitted the required 'available' field", "messaging.md:301"),
    (r"client/state omitted volume despite declaring the volume command", "roles/player/v1.md:45"),
    (
        r"client/state preferred a format not in the client's declared supported_formats",
        "roles/player/v1.md:51",
    ),
    (r"client/state reported available: false before client-stream/end", "roles/source/v1.md:73"),
    (
        r"client/state requested visualizer 'spectrum' without a spectrum configuration",
        "roles/visualizer/v1.md:28",
    ),
    (r"client/state sent a non-positive visualizer rate_max", "roles/visualizer/v1.md:27"),
    (r"client/state source signal sent without line_sense support", "roles/source/v1.md:40"),
    (
        r"client/state used legacy player\.state instead of top-level available",
        "messaging.md:28, messaging.md:303",
    ),
    (r"client/state used the legacy top-level 'state' field", "messaging.md:28, messaging.md:303"),
    (
        r"client/state used the pre-rename 'static_delay_ms' key",
        "messaging.md:28, roles/player/v1.md:47",
    ),
    (r"client/state visualizer types contained non-string entries", "roles/visualizer/v1.md:26"),
    (r"client dialed the server while advertising _sendspin\._tcp", "connection.md:21"),
    (r"client sent client_stream/end, superseded by client-stream/end", "roles/source/v1.md:53"),
    (
        r"client sent client_stream/start, superseded by client-stream/start",
        "roles/source/v1.md:106",
    ),
    (r"client-stream/start FLAC codec_header must contain STREAMINFO", "roles/source/v1.md:93"),
    (
        r"client-stream/start announced a pcm bit_depth that is not a whole number of bytes",
        "roles/source/v1.md:88",
    ),
    (r"client-stream/start announced an unsupported bit_depth", None),
    (r"client-stream/start announced an unsupported channels count", None),
    (r"client-stream/start announced an unsupported sample_rate", None),
    (r"client-stream/start codec_header is not valid Base64", "roles/source/v1.md:96"),
    (r"client-stream/start sent a codec_header for opus", "roles/source/v1.md:94"),
    (r"client-stream/start sent a codec_header for pcm", "roles/source/v1.md:92"),
    (r"client-stream/start sent without a preceding source start command", "roles/source/v1.md:63"),
    (r"client_kc is not canonical base64url", "pairing.md:381"),
    (r"commit_B is not canonical base64url", "pairing.md:341"),
    (r"connected unencrypted \(transition mode\)", "connection.md:66, messaging.md:16"),
    (
        r"did not send the (player|source|artwork|visualizer) client/state object after server/activate in time",
        "messaging.md:297",
    ),
    (r"did not send client/goodbye after server/unpair in time", "messaging.md:456"),
    (
        r"did not send the required initial client/state in time",
        "messaging.md:297, messaging.md:299",
    ),
    (r"fragment used legacy binary message IDs 2/3", "messaging.md:76-77"),
    (r"initial client/state has an active artwork role but no artwork state", "messaging.md:297"),
    (r"initial client/state has an active player role but no player state", "messaging.md:297"),
    (r"initial client/state has an active source role but no source state", "messaging.md:297"),
    (
        r"initial client/state has an active visualizer role but no visualizer state",
        "messaging.md:297",
    ),
    (r"initial client/state omitted the required 'available' field", "messaging.md:301"),
    (r"long_term_psk is not canonical base64url", "pairing.md:390"),
    (r"nonce_B is not canonical base64url", "messaging.md:28, pairing.md:382"),
    (r"pair/abort sent the pre-rename pin_mismatch reason", "pairing.md:403, pairing.md:407"),
    (r"pake_msg_2 is not canonical base64url", "pairing.md:361"),
    (
        r"sent (client/pair-auth|client/pair-confirm|client/pair-finalize|client/pair-retry) before client/pair-init",
        "pairing.md:325",
    ),
    (
        r"sent (client/pair-init|client/pair-pending) with a pairing_index ahead of the server's count",
        "pairing.md:327",
    ),
    (
        r"sent (client/pair-pending|client/pair-init|client/pair-finalize|client/pair-auth|client/pair-confirm|client/pair-retry|pair/abort) before any pairing server/activate",
        "pairing.md:325",
    ),
    (r"sent a malformed (?!client/state\Z|client/command\b)(.+)", "messaging.md:24"),
    (r"sent a controller command the server did not offer", "roles/controller/v1.md:13"),
    (r"sent a malformed client/command controller object", "roles/controller/v1.md:12-17"),
    (r"sent a malformed client/state", "messaging.md:303-309"),
    (r"sent a message before the first server/activate", "messaging.md:14"),
    (r"sent a message before the server/activate that follows a re-handshake", "connection.md:153"),
    (r"sent a message that is not a valid message envelope", "messaging.md:24"),
    (r"sent a message without a payload object", "messaging.md:24"),
    (
        r"sent a pcm source audio chunk that is not a whole number of frames",
        "roles/source/v1.md:92",
    ),
    (r"sent a second client/hello after the hello exchange", "messaging.md:198"),
    (r"sent a server-to-client binary message type", "messaging.md:36"),
    (r"sent a server-to-client message", "messaging.md:26"),
    (r"sent a source audio chunk longer than 150 ms", "roles/source/v1.md:114"),
    (r"sent a source audio chunk shorter than its header", "roles/source/v1.md:108-109"),
    (r"sent a source audio chunk that failed to decode", "roles/source/v1.md:90"),
    (
        r"sent a stream/request-format artwork object, superseded by the client/state artwork object",
        "roles/artwork/v1.md:11",
    ),
    (
        r"sent a stream/request-format player object, superseded by the client/state player format",
        "roles/player/v1.md:51",
    ),
    (
        r"sent a stream/request-format visualizer object, superseded by the client/state visualizer object",
        "roles/visualizer/v1.md:19",
    ),
    (r"sent a stream/request-format without a role object", None),
    (r"sent an empty flac or opus source audio chunk", "roles/source/v1.md:93-94"),
    (r"sent an unsolicited management/result", None),
    (r"sent client/init after the connection was established", "messaging.md:143"),
    (r"sent client/pair-retry past the pairing round limit", "pairing.md:185"),
    (r"sent management/result \(deprecated management activity\)", None),
    (r"sent noise/handshake outside a re-handshake", "messaging.md:162, messaging.md:177"),
    (r"sent source audio while reporting available: false", "roles/source/v1.md:104"),
    (r"sent source audio without an open input stream", "roles/source/v1.md:104"),
    (
        r"stream/request-format artwork channel: format, width and height are required for source (album|artist)",
        "roles/artwork/v1.md:13-15",
    ),
    (
        r"stream/request-format artwork dimensions must be positive: (width|height|width, height)",
        None,
    ),
    (r"stream/request-format artwork requested the removed 'bmp' format", "roles/artwork/v1.md:13"),
    (
        r"stream/request-format artwork used pre-rename dimension keys: (.+)",
        "messaging.md:28, roles/artwork/v1.md:14-15",
    ),
    (
        r"stream/request-format requested a format not in the client's declared supported_formats",
        "roles/player/v1.md:51",
    ),
    (
        r"stream/request-format set buffer_capacity, not a visualizer field here",
        "messaging.md:28, roles/visualizer/v1.md:15",
    ),
    (r"stream/request-format targeted an unknown artwork channel", None),
    (
        r"stream/request-format visualizer fields must be positive: (rate_max|buffer_capacity|rate_max, buffer_capacity)",
        "roles/visualizer/v1.md:27",
    ),
    (r"unencrypted client/hello omitted required version", "messaging.md:146"),
    (r"wrapped_nonce_B is not canonical base64url", "pairing.md:382"),
    (r"wrapped_psk is not canonical base64url", "pairing.md:391"),
)
_COMPILED = tuple((re.compile(pattern), ref) for pattern, ref in _REFS)


class UnmappedReasonError(LookupError):
    """Raised for a reason no pattern matches, which means the bundled copy changed."""


def spec_ref_for(reason: str) -> str | None:
    """
    Return the spec reference for an aiosendspin non-compliance `reason`.

    Returns None when rc1 has no rule for it. Raises UnmappedReasonError when the
    reason matches no known pattern.
    """
    for pattern, ref in _COMPILED:
        if pattern.fullmatch(reason):
            return ref
    raise UnmappedReasonError(reason)
