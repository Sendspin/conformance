"""Scenario registry and lookup helpers."""

from __future__ import annotations

from .models import ScenarioSpec


CLIENT_INITIATED_PCM = ScenarioSpec(
    id="client-initiated-pcm",
    display_name="Client initiates connection and client wants PCM",
    description=(
        "Start the server first, then the client. The client discovers or looks up the "
        "server, initiates the WebSocket connection, advertises PCM as its only supported "
        "audio format, streams audio derived from almost_silent.flac, and compares "
        "canonical PCM hashes."
    ),
    initiator_role="client",
    preferred_codec="pcm",
    required_role_families=("player",),
    verification_mode="audio-pcm",
    scenario_revision=5,
)


SERVER_INITIATED_PCM = ScenarioSpec(
    id="server-initiated-pcm",
    display_name="Server initiates connection and client wants PCM",
    description=(
        "Start the server first, then the client. The client advertises a listener and "
        "PCM as its only supported audio format, the server connects in, streams audio "
        "derived from almost_silent.flac, disconnects, and the matrix compares canonical "
        "PCM hashes."
    ),
    initiator_role="server",
    preferred_codec="pcm",
    required_role_families=("player",),
    verification_mode="audio-pcm",
    scenario_revision=5,
)


SERVER_INITIATED_PROTOCOL_BASELINE = ScenarioSpec(
    id="server-initiated-protocol-baseline-v1",
    display_name="Server initiates a protocol baseline session",
    description=(
        "Start the server first, then the client. The client advertises a listener and "
        "PCM player support. The server connects, and both adapters record the handshake, "
        "role activation, initial state, stream negotiation, and timestamped player chunks "
        "for checks against the pinned spec revision. This test does not check audio "
        "decoding or playback."
    ),
    initiator_role="server",
    preferred_codec="pcm",
    required_role_families=("player",),
    verification_mode="protocol",
    scenario_revision=4,
    protocol_assertions=("CORE-001", "CORE-002", "CORE-003", "CORE-004", "PLAYER-001"),
)


SERVER_INITIATED_FLAC = ScenarioSpec(
    id="server-initiated-flac",
    display_name="Server initiates connection and client wants FLAC",
    description=(
        "Start the server first, then the client. The server loads the PCM audio derived "
        "from almost_silent.flac, the client advertises a listener and FLAC as its only "
        "supported audio format, the server connects in, uses the SDK to encode the PCM "
        "into FLAC, streams it to the client, and the matrix compares the transported "
        "FLAC header and chunk bytes as received by the client. Where the client reports "
        "the audio it decoded and the stream kept the source's sample rate and channel "
        "count, the matrix also compares its canonical PCM hash against the source clip, "
        "so a stream that arrived intact but incomplete fails."
    ),
    initiator_role="server",
    preferred_codec="flac",
    required_role_families=("player",),
    verification_mode="audio-encoded-bytes",
    scenario_revision=5,
)


SERVER_INITIATED_OPUS = ScenarioSpec(
    id="server-initiated-opus",
    display_name="Server initiates connection and client prefers OPUS",
    description=(
        "Start the server first, then the client. The server loads the PCM audio derived "
        "from almost_silent.flac, the client advertises a listener and lists OPUS first "
        "in its supported audio formats, followed by the PCM or FLAC entry every player "
        "must list. The server connects in and, being able to produce OPUS, is expected "
        "to honour that priority: it uses the SDK to encode the PCM into OPUS and streams "
        "it to the client. The matrix verifies the declared list, that the negotiated "
        "format is OPUS, and that the transported OPUS chunk bytes match as received by "
        "the client. OPUS is lossy, so decoded audio is not compared against the source."
    ),
    initiator_role="server",
    preferred_codec="opus",
    required_role_families=("player",),
    verification_mode="audio-encoded-bytes",
    scenario_revision=5,
    verifies_format_priority=True,
)


METADATA_EXTRA_CLI_ARGS = (
    ("metadata_title", "Almost Silent"),
    ("metadata_artist", "Sendspin Conformance"),
    ("metadata_album_artist", "Sendspin"),
    ("metadata_album", "Protocol Fixtures"),
    ("metadata_artwork_url", "https://example.invalid/almost-silent.jpg"),
    ("metadata_year", "2026"),
    ("metadata_track", "1"),
    ("metadata_track_progress", "12000"),
    ("metadata_track_duration", "180000"),
    ("metadata_playback_speed", "1000"),
)


SERVER_INITIATED_METADATA = ScenarioSpec(
    id="server-initiated-metadata",
    display_name="Server initiates connection and client wants Metadata",
    description=(
        "Start the server first, then the client. The client advertises a listener, the "
        "server connects in, sends a metadata state update, disconnects, and the matrix "
        "compares a normalized metadata snapshot and checks that the first state carrying "
        "a metadata object bore a past or present timestamp."
    ),
    initiator_role="server",
    preferred_codec="none",
    required_role_families=("metadata",),
    verification_mode="metadata",
    scenario_revision=4,
    extra_cli_args=METADATA_EXTRA_CLI_ARGS,
)


SERVER_INITIATED_CONTROLLER = ScenarioSpec(
    id="server-initiated-controller",
    display_name="Server initiates connection and client wants Controller",
    description=(
        "Start the server first, then the client. The client advertises a listener, the "
        "server connects in, observes controller state, receives a control command, "
        "disconnects, and the matrix verifies the recorded command."
    ),
    initiator_role="server",
    preferred_codec="none",
    required_role_families=("controller",),
    verification_mode="controller",
    scenario_revision=4,
    extra_cli_args=(
        ("controller_command", "next"),
        ("controller_repeat", "all"),
        ("controller_shuffle", "false"),
    ),
)


SERVER_INITIATED_ARTWORK = ScenarioSpec(
    id="server-initiated-artwork",
    display_name="Server initiates connection and client wants Artwork",
    description=(
        "Start the server first, then the client. The client advertises a listener, the "
        "server connects in, streams album artwork, disconnects, and the matrix compares "
        "the received bytes against the server's encoded artwork."
    ),
    initiator_role="server",
    preferred_codec="none",
    required_role_families=("artwork",),
    verification_mode="artwork",
    scenario_revision=4,
    extra_cli_args=(
        ("artwork_format", "jpeg"),
        ("artwork_width", "256"),
        ("artwork_height", "256"),
    ),
)


SERVER_INITIATED_PCM_24BIT = ScenarioSpec(
    id="server-initiated-pcm-24bit",
    display_name="Server initiates connection and client wants 24-bit PCM",
    description=(
        "Start the server first, then the client. The server emits PCM in the 24-bit "
        "packed wire format (3-byte packed, little-endian, two's complement). The "
        "client advertises a listener and 24-bit PCM as its only supported audio "
        "format. The matrix compares canonical PCM hashes after the client unpacks "
        "the 24-bit stream. A client SDK with no 24-bit decode path misreads the "
        "bytes and produces a hash mismatch."
    ),
    initiator_role="server",
    preferred_codec="pcm",
    required_role_families=("player",),
    verification_mode="audio-pcm",
    scenario_revision=5,
)


CLIENT_INITIATED_STATE_FORMAT_PCM = ScenarioSpec(
    id="client-initiated-state-format-pcm",
    display_name="Client changes its preferred PCM bit depth via client/state",
    description=(
        "Start the server first, then the client. The client connects, advertises two PCM "
        "formats (24-bit then 16-bit), and the server starts streaming the higher-priority "
        "24-bit format. The client then sends a client/state whose player format prefers "
        "the 16-bit entry. The matrix verifies the server received that preference in "
        "client/state and the client observed a new stream/start in the 16-bit format."
    ),
    initiator_role="client",
    preferred_codec="pcm",
    required_role_families=("player",),
    verification_mode="format-preference",
    scenario_revision=4,
)


CLIENT_INITIATED_STATE_FORMAT_FLAC = ScenarioSpec(
    id="client-initiated-state-format-flac",
    display_name="Client changes its preferred format from PCM to FLAC via client/state",
    description=(
        "Start the server first, then the client. The client connects, advertises PCM then "
        "FLAC, and the server starts streaming the higher-priority PCM format. The client "
        "then sends a client/state whose player format prefers the FLAC entry. The matrix "
        "verifies the server received that preference in client/state and the client "
        "observed a new stream/start in the FLAC format."
    ),
    initiator_role="client",
    preferred_codec="flac",
    required_role_families=("player",),
    verification_mode="format-preference",
    scenario_revision=4,
)


SERVER_INITIATED_LEGACY_UNENCRYPTED = ScenarioSpec(
    id="server-initiated-legacy-unencrypted",
    display_name="Server initiates connection over the legacy unencrypted transition mode",
    description=(
        "Start the server first, then the client. The server uses aiosendspin's "
        "allow_unencrypted mode to test clients that do not yet support the required "
        "Noise handshake. This mode is outside the spec and sends traffic without "
        "encryption. The test can be removed once all tracked implementations support "
        "the Noise handshake."
    ),
    initiator_role="server",
    preferred_codec="pcm",
    required_role_families=("player",),
    verification_mode="audio-pcm",
    scenario_revision=5,
    requires_legacy_unencrypted=True,
)


SERVER_INITIATED_AUDIO_CHUNK_FRAMING = ScenarioSpec(
    id="server-initiated-audio-chunk-framing",
    display_name="Server initiates connection and frames PCM audio chunks",
    description=(
        "Start the server first, then the client. The client advertises a listener and "
        "PCM as its only supported audio format, the server connects in and streams "
        "audio derived from almost_silent.flac, and both adapters record each audio "
        "frame as it crossed the transport. The matrix verifies that every frame is "
        "message type 4 behind the 13-byte header (type, int64 timestamp, uint32 "
        "send_ahead), that the client read the same 13 bytes off each one, and that no "
        "chunk lasted longer than 150 ms or, the final chunk aside, less than 15 ms. "
        "This test does not compare the audio, and does not judge send_ahead "
        "saturation, which no adapter can observe."
    ),
    initiator_role="server",
    preferred_codec="pcm",
    required_role_families=("player",),
    verification_mode="audio-chunk-framing",
    scenario_revision=3,
)


SCENARIO_LIST: tuple[ScenarioSpec, ...] = (
    CLIENT_INITIATED_PCM,
    SERVER_INITIATED_PCM,
    SERVER_INITIATED_PROTOCOL_BASELINE,
    SERVER_INITIATED_METADATA,
    SERVER_INITIATED_ARTWORK,
    SERVER_INITIATED_CONTROLLER,
    SERVER_INITIATED_FLAC,
    SERVER_INITIATED_OPUS,
    SERVER_INITIATED_PCM_24BIT,
    CLIENT_INITIATED_STATE_FORMAT_PCM,
    CLIENT_INITIATED_STATE_FORMAT_FLAC,
    SERVER_INITIATED_LEGACY_UNENCRYPTED,
    SERVER_INITIATED_AUDIO_CHUNK_FRAMING,
)

SCENARIOS: dict[str, ScenarioSpec] = {scenario.id: scenario for scenario in SCENARIO_LIST}


def ordered_scenarios() -> tuple[ScenarioSpec, ...]:
    """Return scenarios in display/run order."""
    return SCENARIO_LIST


def get_scenario(scenario_id: str) -> ScenarioSpec | None:
    """Return a registered scenario by ID."""
    return SCENARIOS.get(scenario_id)


def require_scenario(scenario_id: str) -> ScenarioSpec:
    """Resolve a scenario or raise a descriptive error."""
    scenario = get_scenario(scenario_id)
    if scenario is None:
        raise ValueError(f"Unknown scenario: {scenario_id}")
    return scenario
