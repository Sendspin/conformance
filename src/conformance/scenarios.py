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
    scenario_revision=2,
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
    scenario_revision=2,
)


SERVER_INITIATED_PROTOCOL_BASELINE = ScenarioSpec(
    id="server-initiated-protocol-baseline-v1",
    display_name="Server initiates a protocol baseline session",
    description=(
        "Start the server first, then the client. The client advertises a listener and a "
        "PCM player capability, the server connects in, and both adapters record "
        "spec-revision-pinned evidence for the handshake, role activation, initial state, "
        "stream negotiation, and timestamped player chunks. Media decoding and rendered "
        "audio are explicitly outside this test."
    ),
    initiator_role="server",
    preferred_codec="pcm",
    required_role_families=("player",),
    verification_mode="protocol",
    scenario_revision=1,
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
    scenario_revision=2,
)


SERVER_INITIATED_OPUS = ScenarioSpec(
    id="server-initiated-opus",
    display_name="Server initiates connection and client wants OPUS",
    description=(
        "Start the server first, then the client. The server loads the PCM audio derived "
        "from almost_silent.flac, the client advertises a listener and OPUS as its only "
        "supported audio format, the server connects in, uses the SDK to encode the PCM "
        "into OPUS, streams it to the client, and the matrix compares the transported "
        "OPUS header and chunk bytes as received by the client."
    ),
    initiator_role="server",
    preferred_codec="opus",
    required_role_families=("player",),
    verification_mode="audio-encoded-bytes",
    scenario_revision=1,
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
    scenario_revision=2,
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
    scenario_revision=2,
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
    scenario_revision=1,
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
    scenario_revision=2,
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
    scenario_revision=1,
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
    preferred_codec="pcm",
    required_role_families=("player",),
    verification_mode="format-preference",
    scenario_revision=1,
)


SERVER_INITIATED_LEGACY_UNENCRYPTED = ScenarioSpec(
    id="server-initiated-legacy-unencrypted",
    display_name="Server initiates connection over the legacy unencrypted transition mode",
    description=(
        "Start the server first, then the client. The server opts into aiosendspin's "
        "non-spec unencrypted transition mode (allow_unencrypted) rather than the real "
        "encrypted Noise handshake, to keep tracking the plaintext legacy path used by "
        "implementations that have not yet adopted the spec's mandatory encryption. This "
        "scenario is expected to be dropped once every tracked implementation supports the "
        "real handshake."
    ),
    initiator_role="server",
    preferred_codec="pcm",
    required_role_families=("player",),
    verification_mode="audio-pcm",
    scenario_revision=2,
    requires_legacy_unencrypted=True,
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
