# AGENTS.md

## Purpose

This repository is a conformance harness for multiple Sendspin implementations. It does four things:

1. builds adapter CLIs for each implementation/toolchain
2. runs a scenario matrix across `server -> client` implementation pairs
3. records per-case artifacts and summaries
4. generates a static HTML report for GitHub Pages

Treat this repository as infrastructure around the implementations, not as the implementations themselves.

## First read

Start with these files before making changes:

- `README.md`: quick start for validating a client app and running the harness locally
- `src/conformance/scenarios.py`: canonical list and order of scenarios
- `src/conformance/implementations.py`: implementation registry and role capability metadata
- `src/conformance/runner.py`: matrix execution and artifact layout
- `src/conformance/site.py`: static report generation
- `adapters/README.md`: stable adapter CLI contract

## Core invariants

These are the easiest things to break accidentally.

### Adapter model

Every implementation is modeled as exactly two CLIs:

- `server`
- `client`

An implementation whose role is not `supported` at all gets no cases in that role: the runner skips it before case creation and the report draws no row (server) or column (client) for it, so a server-only or client-only implementation adds no dead cells to the matrix. Both the runner and the site take the client list from `implementations_with_role()` and the server list from `implementations_for_scenario()`.

The filters differ per side. Server rows are also dropped per scenario, wherever `RoleSpec.unsupported_reason()` returns a reason. Client columns are not: a client that implements the role but cannot run one scenario still gets a case, which fails fast through the placeholder adapter and reads as "Unsupported" in that column.

When the harness judges a case unsupported — the role is not `supported`, or
`RoleSpec.unsupported_reason()` returns a reason for the scenario — it stamps
`unsupported: true` on the result row. The report labels and counts a failed cell
as unsupported from that field alone, never from the wording of a `reason`, so an
adapter cannot relabel its own failure. `status` stays `failed`. A row published
before the field existed carries none and reads as a plain failure.

Adapters do not need to rely on the implementation library owning discovery itself.
External mDNS advertisement/browsing or the harness registry handoff are acceptable,
as long as the adapter can still attach the implementation to an outbound WebSocket
or an accepted inbound WebSocket and report the resulting interaction honestly.

### Scenario model

Scenarios are data-driven for matrix construction, ordering, and report rendering.
Adapter behavior is intentionally explicit per scenario, so hard-coded
`scenario_id` branching in adapters is acceptable and expected.

Important current scenario traits:

- `initiator_role`
- `preferred_codec`

Adapters should hardcode supported scenario IDs and scenario-specific behavior.
Use generic traits (`initiator_role`, `preferred_codec`, etc.) as helpful inputs,
but do not treat them as a replacement for explicit per-scenario handling.

### Scenario ordering

Scenario order is explicit and matters:

- run order
- site index order
- user perception of report health

Today the PCM scenarios are intentionally listed first because they are greener. Preserve that unless there is a strong reason to change it.

### Result layout

The report output layout is fixed:

- `results/data/`
- `results/index.html`
- `results/implementations/*.html`
- `results/scenarios/*.html`
- `results/cases/*.html`

Per-case artifacts live under `results/data/<environment>__<scenario>__<server>__to__<client>/`.
Cross-run metadata such as build logs and repository revisions also live under
`results/data/`, notably:

- `results/data/build-report.json`
- `results/data/repositories.json`
- `results/data/declared-formats.json`

`declared-formats.json` is derived by the report step from the per-case summaries,
so a merged multi-host run regenerates it rather than merging it. It carries a
`schema_version`; downstream consumers should check it.

`repositories.json` leads with a `spec` entry recording the revision of the
Sendspin spec the run was audited against, followed by one entry per
implementation the run covered. Every entry carries `revision_label`, a
`git describe` label that is a tag when the checkout sits on one. A merged
multi-host run keeps one entry per host, so `spec` is not unique there.

Do not reintroduce SPA-style routing or inline all case details onto scenario pages.

### Report behavior

The site is static HTML, not a SPA. The index page shows all scenario matrices. Each scenario gets its own HTML page, and each concrete server/client pairing gets its own dedicated HTML page. Use only minimal vanilla JS; today that is limited to the case-page tabs in `src/conformance/site.py`.

Keep server/client roles explicit everywhere in the report. Do not regress back to ambiguous `from/to` terminology.

The generated site also shows:

- the audio formats each client declared in `client/hello`, marked exercised or not verified, on that client's implementation page
- per-case status and reason with explicit server/client labeling
- summary, server, client, and build tabs on each dedicated case page when build data exists
- linked case artifacts for drill-down: `result.json`, client/server summaries, and logs

The overview page also shows repository revision metadata gathered at run time.
If you change how repositories are resolved, merged, or reported, update both
`src/conformance/repository_versions.py` and the overview rendering in
`src/conformance/site.py`.

### Summary contract

`unverifiable` is a final case status, never a reason to skip execution. Run both
adapters and check the available evidence. Use it only when an SDK cannot expose
decoded PCM, raw client audio frames or baseline protocol evidence, or when
resampling prevents comparison against the source PCM hash. Adapters report
`status: "ok"` and explicit `null` for unavailable evidence. Missing fields and
malformed evidence remain harness failures. Connection errors, nonzero exits and
observed protocol violations still fail. The site counts unverifiable cases
separately; regression detection treats them as not passed.

Successful adapters should print a JSON summary and exit `0`.

Important fields to preserve where available:

- `status`
- `implementation`
- `role`
- `scenario_id`
- `initiator_role`
- `preferred_codec`
- `peer_hello`
- `activation` (server summaries)
- `availability_trace` (server summaries)
- `time_exchange` (server summaries)
- `group_updates` (server summaries)
- audio hash fields

`peer_hello` should contain the full hello message received from the other party whenever capture is possible.

`activation` should contain the first `server/activate` the server sent, as it went on the wire and in the same `{type, payload}` shape as `peer_hello`. It is `null` only when the server sent none, and is never reconstructed from adapter arguments or SDK state.

`null` fails the case: `activation_violation` in `src/conformance/protocol.py` judges this field on every case, whatever the scenario.

`group_updates` should contain every `group/update` the server sent after that first `server/activate`, in order, each as it went on the wire and in the same `{type, payload}` shape. It is an empty list only when none followed, and is never reconstructed from adapter arguments or SDK state.

`group_update_violation` in `src/conformance/protocol.py` judges this field on every case too. An empty list fails the case, and so does any recorded `group/update` whose `playback_state` is not `playing` or `stopped`, or whose `group_id` or `group_name` is missing or not a string. The group ID must also contain exactly 43 base64url characters. Unlike `activation`, a summary without the field also fails the case, as a harness gap. The difference is deliberate, so do not align the two. Every server adapter in the tree records `group_updates`, so a summary without it comes from a broken adapter, never from an implementation that predates the field, and failing closed keeps an adapter that cannot observe the messages from reading as a server that sent them. How soon the first message followed is not judged, because the spec says "promptly" and gives no bound, so one sent at any point in the case satisfies it.

Every recorded `group/update` is judged for its fields. The same spec sentence also requires one "whenever any field listed below changes", and nothing in the matrix asserts that a change produced one.

`availability_trace` should contain each `client/state` the server received and each `stream/start` it sent, in the order the adapter observed them, as `adapters/README.md` specifies. It is `null` only when the adapter cannot observe that ordering, and is never reconstructed from what the implementation is expected to do.

`null` or a missing field fails the case as a harness gap: `stream_start_gate_violation` in `src/conformance/protocol.py` judges this field on every scenario that opens a player or artwork stream.

`time_exchange` should contain each `client/time` the server received and each `server/time` it sent, in the order the adapter observed them, each as it went on the wire and in the same `{type, payload}` shape, with an `other-sent` entry where the server began sending any other text message, as `adapters/README.md` specifies. It is an empty list when the client sent no `client/time`, `null` only when the adapter cannot observe the messages, and is never reconstructed from adapter arguments or SDK state.

`time_exchange_violation` in `src/conformance/protocol.py` judges this field on every case. `null` or a missing field fails the case as a harness gap, and an empty list gives it nothing to judge. It does not judge the timestamp values, how late `server_transmitted` was stamped, or whether the client's time filter converged, and `adapters/README.md` gives the reason for each.

### Audio fixture

The shared source fixture is `almost_silent.flac` from `sendspin-cli`:

- resolved by `src/conformance/fixtures.py`
- expected at `sendspin-cli/tests/fixtures/almost_silent.flac`

Do not hard-code another fixture path in adapters or tests.

### CI expectation

GitHub Actions should still publish artifacts and GitHub Pages even when the conformance matrix has failing cases. Harness failures are currently expected. Do not add a workflow step that turns expected red matrix cases into a failed workflow.

Build/setup failures are different: those can still be real workflow failures.

Two workflows run on pull requests and pushes to `main`:

- `publish.yml` builds every implementation, runs the full matrix on macOS and publishes Pages. Only changes that can alter the matrix or the site start it: `src/conformance/**`, `adapters/**`, `scripts/**`, `pyproject.toml`, `uv.lock` and the workflow itself. The client validator (`src/conformance/validator/**`), its vendored aiosendspin (`src/conformance/_vendor/**`) and `scripts/vendor_aiosendspin.py` are excluded, because the matrix never runs them. Changes to `tests/`, docs or other workflows do not start it either. The nightly schedule and manual dispatch always run it.
- `ci.yml` runs `scripts/vendor_aiosendspin.py --check` and `tests/` on Linux whenever `src/`, `tests/`, `scripts/`, `patches/`, `pyproject.toml`, `uv.lock` or the workflow itself changes. Checks that need no implementation clones or macOS toolchains belong here.

A new path the matrix depends on must be added to `publish.yml`'s path filters, or changes to it will merge without a matrix run.

The published workflow currently runs on `macos-latest` and publishes one matrix without host-specific sections in the UI. Internal environment metadata may still exist in raw artifacts for build-log lookup or local merged runs, but the public report should read as one authoritative matrix.

### Regression detection

After building the report, CI compares local results against the live published baseline at `https://sendspin.github.io/conformance/data/index.json`. A *regression* is any case that was `passed` in the baseline but is no longer `passed` locally.

- **Pull requests**: the workflow fails if regressions are detected.
- **Pushes to main / scheduled / manual**: regressions are logged and a Discord notification text is uploaded as an artifact (`discord-regression-notice`), but the workflow does not fail so that Pages still publishes.

If the baseline is unreachable (first publish, network error), the check is skipped silently.

Changing what a scenario does — tightening its assertions, or altering the
stimulus it drives the pair with — turns its cells red by design, which is not a
regression. Each `ScenarioSpec` carries a `scenario_revision`, stamped onto every
result row, and a cell is exempt from comparison when its baseline row carries a
different revision. Exempt cells are always printed, with their reason.

A baseline published before the field existed carries no revision on any row, so
every cell is exempt for that one release. That exemption retires itself: once
*any* baseline row carries a revision, a row missing one is read as a broken
contract rather than an old publish, and the cell keeps its protection. A local
result row missing a revision fails the check outright — it is written by the
harness in the same checkout, so its absence is a bug, not a migration. The
blanket is re-armable: replacing the published baseline with one built from a
checkout that predates the field exempts every cell again for that release. It
announces itself — the whole matrix prints the migration reason — and closing it
costs more than the risk.

Bumping `scenario_revision` on the one scenario that changed is the deliberate
act that resets its baseline; every other scenario in the same run
keeps full protection. A verdict applied to every case, such as
`activation_violation`, `group_update_violation` or
`time_exchange_violation`, changes the assertions of every scenario and so
bumps them all, which exempts the whole matrix for that one release. A
verdict applied to some scenarios, such as
`stream_start_gate_violation`, bumps only those. Do not add a
waiver file, allowlist or PR-label escape hatch — the baseline is live and
remote, so any such list goes stale on the next merge.

## Scenarios and coverage

`src/conformance/scenarios.py` is the source of truth for the scenario list and order, and `src/conformance/implementations.py` for coverage. Keep this section in step with them when either changes.

### Current scenarios

- `client-initiated-pcm` (Client initiates connection and client wants PCM): start the server first, let the client discover/connect to it, advertise PCM as the only supported audio format, and compare canonical PCM hashes
- `server-initiated-pcm` (Server initiates connection and client wants PCM): start the server first, let the client advertise a listener and PCM as its only supported audio format, let the server connect in, and compare canonical PCM hashes
- `server-initiated-protocol-baseline-v1` (Server initiates a protocol baseline session): validates a spec-revision-pinned protocol trace for handshake order, activation, initial state, stream negotiation, and player chunk framing; it does not assess decoded or rendered audio
- `server-initiated-metadata` (Server initiates connection and client wants Metadata): start the server first, let the client advertise a listener, let the server connect in, receive a metadata snapshot, and compare normalized metadata fields
- `server-initiated-artwork` (Server initiates connection and client wants Artwork): start the server first, let the client advertise a listener, let the server connect in, receive album artwork bytes, and compare the encoded image hash
- `server-initiated-controller` (Server initiates connection and client wants Controller): start the server first, let the client advertise a listener, let the server connect in, observe controller state, send a control command, and verify the server recorded it
- `server-initiated-flac` (Server initiates connection and client wants FLAC): start the server first with PCM audio decoded from `almost_silent.flac`, let the client advertise a listener and FLAC as its only supported audio format, let the server connect in, encode the PCM to FLAC using the SDK, stream it to the client, and compare the transported FLAC bytes; where the client reports the audio it decoded and the stream kept the source's sample rate and channel count, also compare its canonical PCM hash against the source clip, so an intact but incomplete stream fails
- `server-initiated-opus` (Server initiates connection and client prefers OPUS): start the server first with PCM audio decoded from `almost_silent.flac`, let the client advertise a listener and list OPUS first in its supported audio formats, followed by the PCM or FLAC entry every player must list, let the server connect in, and verify that it honoured that priority by encoding the PCM to OPUS using the SDK and streaming it to the client; compare the declared list, the negotiated format and the transported OPUS bytes. OPUS is lossy, so decoded audio is not compared against the source clip
- `server-initiated-pcm-24bit` (Server initiates connection and client wants 24-bit PCM): start the server first, let the client advertise a listener and 24-bit PCM as its only supported audio format, let the server connect in, negotiate the 24-bit packed wire format, stream it to the client, compare canonical PCM hashes, and verify that the negotiated stream was 24-bit — a client SDK with no 24-bit decode path misreads the bytes and produces a hash mismatch, and a pair that streamed another depth has not tested the 24-bit path
- `client-initiated-state-format-pcm` (Client changes its preferred PCM bit depth via `client/state`): start the server first, let the client connect advertising two PCM formats (24-bit then 16-bit), let the server stream the higher-priority 24-bit format, have the client send a `client/state` whose player `format` prefers the 16-bit entry, and verify the server received that preference in `client/state` and the client observed a new `stream/start` in the 16-bit format
- `client-initiated-state-format-flac` (Client changes its preferred format from PCM to FLAC via `client/state`): start the server first, let the client connect advertising PCM then FLAC, let the server stream the higher-priority PCM format, have the client send a `client/state` whose player `format` prefers the FLAC entry, and verify the server received that preference in `client/state` and the client observed a new `stream/start` in the FLAC format
- `client-initiated-metadata` (Client initiates connection and client wants Metadata): start the server first, let the client discover/connect to it, receive a metadata snapshot, and compare normalized metadata fields
- `client-initiated-artwork` (Client initiates connection and client wants Artwork): start the server first, let the client discover/connect to it, receive album artwork bytes, and compare the encoded image hash
- `client-initiated-controller` (Client initiates connection and client wants Controller): start the server first, let the client discover/connect to it, observe controller state, send a control command, and verify the server recorded it
- `server-initiated-audio-chunk-framing` (Server initiates connection and frames PCM audio chunks): start the server first, let the client advertise a listener and PCM as its only supported audio format, let the server connect in and stream, and have both adapters record each audio frame as it crossed the transport; verify that every frame is message type 4 behind the 13-byte header (type, int64 timestamp, uint32 `send_ahead`), that the client read the same 13 bytes off each one, and that no chunk lasted longer than 150 ms or, the final chunk aside, less than 15 ms. The audio itself is not compared, and `send_ahead` saturation is not judged because no adapter can observe when a frame was transmitted
- `server-initiated-legacy-unencrypted` (Server initiates connection over the legacy unencrypted transition mode): start the server first, then the client; the server uses aiosendspin's `allow_unencrypted` mode to test clients that do not yet support the required Noise handshake. This mode is outside the spec and sends traffic without encryption, and the scenario can be removed once every tracked implementation supports the Noise handshake

### Current coverage

- `aiosendspin`: real server adapter, including OPUS encoding, and real client adapter; the client SDK decodes PCM and FLAC only, so it does not advertise OPUS
- `sendspin-dotnet`: real client adapter for client-initiated PCM plus the server-initiated PCM, metadata, artwork, controller, and FLAC scenarios; server placeholder
- `SendspinKit`: real Swift client adapter for client-initiated PCM, Flac, and Opus plus the server-initiated PCM, Flac, Opus, metadata, artwork, and controller scenarios; server placeholder
- `sendspin-cpp`: real C++ client adapter for client-initiated PCM plus the server-initiated PCM, 24-bit PCM, metadata, artwork, controller, FLAC, OPUS, legacy-unencrypted, protocol-baseline and audio-chunk-framing scenarios, evidenced from the library's message trace; it joins the `client-initiated-state-format-*` scenarios as a player but fails them, since the library exposes no way to set the `client/state` player `format`; server placeholder
- `sendspin-go`: not currently tested. Both roles are placeholders: the server library has no RC1 `client/init` or Noise handshake, and the Go client adapter source in `adapters/sendspin-go/` is kept but no longer built or run
- `sendspin-js`: real Node.js client adapter driving the public `SendspinCore` SDK over an adapter-owned WebSocket; covers client-initiated PCM plus the server-initiated PCM, metadata, and controller scenarios (FLAC/OPUS decode and `artwork@v1` are not exposed by the public SDK in a headless Node environment); server placeholder
- `sendspin-rs`: real Rust client adapter for client-initiated PCM plus the server-initiated PCM, metadata, artwork, controller, and FLAC scenarios; server placeholder
- `sendspin-server-cpp`: real C++ server adapter for the client- and server-initiated PCM, 24-bit PCM, protocol-baseline, audio-chunk-framing, metadata, artwork and controller scenarios, the server-initiated FLAC and OPUS scenarios (both encoded by the library) and both `client-initiated-state-format-*` scenarios, built on the library's public API and evidenced from its message trace; the library has no unencrypted mode, so it takes no part in `server-initiated-legacy-unencrypted`; client placeholder (its client counterpart is `sendspin-cpp`)

The OPUS scenario is currently exercised only by the `SendspinKit` and `sendspin-cpp` clients against the `aiosendspin` and `sendspin-server-cpp` servers until other implementations opt in via `supports_opus`.

The `client-initiated-state-format-*` scenarios have no capability flag of their own: RC1 gives every player the `client/state` `format` preference, so a client that never sends one shows as a failed case. The FLAC variant still needs a client that can list FLAC (`supports_flac`). The `aiosendspin` and `SendspinKit` client adapters drive them today.

The `client-initiated-metadata`, `client-initiated-artwork` and `client-initiated-controller` scenarios have no capability flag of their own either: RC1 requires every server to support client-initiated connections, whichever roles the client brings. The `aiosendspin`, `sendspin-dotnet`, `SendspinKit` and `sendspin-cpp` client adapters drive all three over the outbound path they use for `client-initiated-pcm`. The `sendspin-js` client adapter drives the metadata and controller scenarios the same way; it declares no `artwork` role family, so the harness marks its `client-initiated-artwork` cases unsupported before launching anything. The `sendspin-rs` client adapter has an outbound path for player sessions only, so its cases fail as a harness gap rather than being left out.

The `server-initiated-audio-chunk-framing` scenario has no capability flag of its own either. A client adapter that receives parsed chunks and cannot observe raw frames still runs. Its case is unverifiable unless the available evidence shows a failure. Only the `aiosendspin` adapters, the `sendspin-server-cpp` server and the `sendspin-rs` and `sendspin-cpp` clients observe raw frames today.

Which rows and columns appear follows the rule under "Adapter model" above.

### sendspin-js build

The JavaScript adapter builds the selected sendspin-js checkout with its Yarn
lockfile and imports that same checkout. CI clones the latest default branch on
each run. Update existing local checkouts before running the matrix; their exact
revisions appear in the report.

### Protocol conformance migration

The matrix is moving from media-oriented interoperability checks to normative protocol
conformance. `server-initiated-protocol-baseline-v1` is the first authoritative
protocol test and is pinned to Sendspin spec revision
`8c9577ea8719ad082d051ec13cc73ef15ed68948`. That pin scopes this scenario's assertions
only. It is not the revision the matrix is audited against: the verdicts applied to every
case are judged against the spec checkout each run records in `repositories.json` and
shows on the report overview. An adapter that cannot expose ordered protocol evidence reports it as unavailable.
The case still runs and checks the other peer's evidence. The contract is described in
[`adapters/README.md`](adapters/README.md). The current PCM, FLAC, OPUS, artwork,
metadata, and controller scenarios remain diagnostic interoperability checks during
this migration: a matching hash or normalized snapshot is evidence that two adapters
interoperated, not proof that either followed the specification.

## Client validator

`sendspin-validate` checks a Sendspin client app on its own, without an adapter. It
is a strict Sendspin server, built on a bundled copy of aiosendspin, that your app
connects to. It records every frame, checks what the client sends against spec
`1.0.0-rc1`, and writes a report. The only prerequisite is
[uv](https://docs.astral.sh/uv/):

```sh
uvx --from git+https://github.com/Sendspin/conformance sendspin-validate
```

It asks how your app connects. In client-initiated mode it listens on port 8927
(`--port`), announced as `_sendspin-server._tcp`, and judges the first client to
connect. In server-initiated mode it lists the `_sendspin._tcp` clients it finds and
connects to the one you pick, taking it over from any server holding it idle;
`--connect ws://host:port/path` skips mDNS. `--verbose` streams every decoded frame to
stderr, and `--fresh` starts from a new validator identity. The identity is kept in the
platform user-data directory.

On the first connection it checks:

- `client/init`: `client_id`, `version`, `suite`, and no undefined fields
- the Noise handshake: message 2 carries exactly `{}`
- ordering: nothing but `client/hello` or `client/goodbye` before the first `server/activate`
- `client/hello`: support objects, role names, pairing methods, `unpaired_access`, no
  undefined fields, and (a warning only) the mDNS TXT `name`
- the initial `client/state`, its `available` field and its role objects, and `client/time`
- every rule aiosendspin's strict mode enforces, on every connection of the run, each
  cited to the spec line it comes from

It then walks you through the suites `--suite` names, in this order (default: all):

- `unpaired`: U1 the Sentinel handshake; U2 refusing playback with unpaired access
  off (`pairing_required`); U3 accepting it with unpaired access on. U4, an activation
  no client may accept, runs only with `--fault-injection`.
- `pairing`: each method your app's `client/hello` offers, in this order, with the
  checks of a method it does not offer reported `N/A`:
  - P2 in the QR format: you paste the text of the QR your app shows.
  - P1, pairing with the token (`SP:…`) you paste from your app or leaflet, through
    both re-handshakes.
  - P2 in the digits format: you type the 6-digit code your app shows or speaks, then
    a wrong one on purpose, which your app must reject, then the right one again. The
    validator names a random non-English language in `server/hello`, and asks which
    language a speaking app used.
  - P3, the static code: the validator first asks with the pairing window closed, then
    you open it and type the 8-digit code.
  - P4: the validator cancels an attempt, which your app must drop cleanly. Then, if
    you opt in at its prompt, it leaves one idle until your app times it out, which
    takes about two minutes.
  - P3 lockout, if you opt in at its prompt: five wrong static codes, which must close
    your device's pairing window, then a sixth attempt your app must hold back.
- `post-pairing`: R1 reconnecting paired; R3 accepting the Sentinel once the validator
  forgets the pairing; R5 keeping the pairing and output delay across a full restart;
  R4 `server/unpair`, then checking the app really dropped the pairing.

Another server may already hold your app idle, such as a Music Assistant that found
it over mDNS. In server-initiated mode, each pairing step, U2 and U4 open a connection
whose first `server/activate` is the step's own, so the app admits it over the idle
server. Other steps open with an activation that declares nothing. If the app turns
one of those away with `client/goodbye` `concurrent_attempt`, that step's failures
are reported as `TOOL_ERROR`, and the validator says how to keep other servers away.
If the app answers a step's own first activation that way, the failure stands.

Every step waits for you, and every question accepts `s` (skip), `r` (retry) and `?`
(not sure). The hidden `--auto-answer` flag is for the CI self-test only.

The run writes `report.json`, a self-contained `report.html` and `protocol.log.jsonl`
to `./sendspin-validate-<client-name>-<timestamp>/`. Pairing secrets are redacted as
frames are recorded, and so is every code you enter. The exit status is 1 when any
check is `FAIL` or `TOOL_ERROR`.

## Repository map

### Harness code

- `src/conformance/models.py`: shared dataclasses and scenario capability helpers
- `src/conformance/scenarios.py`: scenario registry and order
- `src/conformance/implementations.py`: implementation registry and role metadata
- `src/conformance/runner.py`: matrix execution and process orchestration
- `src/conformance/build.py`: adapter build checks
- `src/conformance/repository_versions.py`: git revision metadata written into `results/data/repositories.json`
- `src/conformance/declared_formats.py`: joins client format declarations to the formats the matrix negotiated, written into `results/data/declared-formats.json`
- `src/conformance/chunk_framing.py`: judges the player audio chunk header and chunk durations from the frames a case transported
- `src/conformance/merge.py`: merges host-specific raw result directories, including build and repository metadata
- `src/conformance/site.py`: static site generation
- `src/conformance/flac.py` and `src/conformance/pcm.py`: canonical decode/hash helpers
- `src/conformance/validator/`: the `sendspin-validate` client validator, a strict bundled-aiosendspin server that records a client's frames and judges them. It imports only `conformance._vendor.aiosendspin` and no harness module that touches `repos/`
- `src/conformance/_vendor/`: aiosendspin bundled for the client validator under its own import name, generated by `scripts/vendor_aiosendspin.py` from upstream plus `patches/aiosendspin/`. Never edit it by hand. The harness and its adapters keep loading `aiosendspin` from `repos/`

### Adapters

- `src/conformance/adapters/aiosendspin_server.py`: real Python server adapter
- `src/conformance/adapters/aiosendspin_client.py`: real Python client adapter
- `adapters/sendspin-dotnet/client/`: real `.NET` client adapter source
- `adapters/sendspin-go/`: Go client adapter source, kept but not currently built or tested
- `adapters/SendspinKit/client/`: real Swift client adapter source with an adapter-owned inbound WebSocket listener for server-initiated scenarios
- `adapters/sendspin-rs/client/`: real Rust client adapter source
- `adapters/sendspin-js/client.mjs`: real Node.js client adapter source that drives the public `SendspinCore` SDK over an adapter-owned WebSocket
- `adapters/sendspin-cpp/client/`: real C++ client adapter source, built against `repos/sendspin-cpp` with CMake, whose audio and protocol evidence comes from the library's message trace
- `adapters/sendspin-server-cpp/server/`: real C++ server adapter source, built against `repos/sendspin-server-cpp` with CMake, whose evidence comes from the library's message trace
- `src/conformance/adapters/placeholder.py`: generic fail-fast adapter

### Scripts

- `scripts/setup_repositories.py`: clone/check required repos
- `scripts/setup_workspace.py`: create local venv and install Python deps
- `scripts/run_all.py`: build + run + report orchestration
- `scripts/merge_results.py`: merge multiple host result sets into one report
- `scripts/vendor_aiosendspin.py`: rebuild `src/conformance/_vendor/` at an upstream ref, or `--check` it against the recorded commit

### CI

- `.github/workflows/publish.yml`: macOS build + run + Pages publishing
- `.github/workflows/ci.yml`: Linux unit tests including the validator self-test, and `scripts/vendor_aiosendspin.py --check` against the committed `src/conformance/_vendor/`
- `scripts/detect_regressions.py`: compares local results against the published baseline to detect regressions

## Standard commands

Bootstrap locally:

```bash
python scripts/setup_workspace.py --clone
. .venv/bin/activate
```

`uv.lock` is committed. CI and `scripts/setup_workspace.py` install with `uv sync --locked`, which fails when the lock no longer matches `pyproject.toml`. Run `uv lock` after editing dependencies and commit the result. Run `uv lock --upgrade` to refresh every pin.

Build adapters:

```bash
python -m conformance.cli build
python -m conformance.cli build --report-path artifacts/build-report.json
```

Run full matrix:

```bash
python scripts/run_all.py --results-dir results --build-report-path artifacts/build-report.json
```

Run with explicit host metadata:

```bash
python scripts/run_all.py \
  --results-dir results \
  --build-report-path artifacts/build-report.json \
  --environment-id linux \
  --environment-name Linux
```

Run filtered matrix:

```bash
python -m conformance.cli run --results-dir results --from aiosendspin --to aiosendspin,sendspin-dotnet
```

Run the matrix with parallel case execution:

```bash
python -m conformance.cli run --jobs 4
python scripts/run_all.py --jobs 4
```

The runner assigns a dedicated server port and client-listener port to each case,
so parallel cases do not fight over `8927`/`8928`.

Regenerate report only:

```bash
python -m conformance.cli report --results-dir results
```

Merge multiple host runs:

```bash
python scripts/merge_results.py \
  --output-dir artifacts/results \
  artifacts/linux-results \
  artifacts/macos-results
```

Fast sanity checks after edits:

```bash
python -m compileall src scripts
git diff --check
```

If you touched the `.NET` adapter, also run:

```bash
~/.dotnet/dotnet build adapters/sendspin-dotnet/client/Conformance.SendspinDotnet.Client.csproj
```

## How to add a new scenario

Prefer this path:

1. Add a `ScenarioSpec` entry in `src/conformance/scenarios.py`.
2. Add explicit adapter handling for the new `scenario_id` in each real adapter that supports it.
3. Reuse generic fields like `initiator_role`, `preferred_codec`, and `extra_cli_args` where useful, but keep scenario coverage explicit per adapter.
4. Keep scenario order intentional.
5. Verify the report renders the new scenario on the index page and generates dedicated pages under `results/scenarios/` and `results/cases/`.

If a scenario requires new behavior that does not fit the existing generic contract, add the minimum new generic scenario field needed while still keeping explicit per-scenario adapter handling.

## How to add or upgrade an implementation adapter

1. Add or update the implementation entry in `src/conformance/implementations.py`.
2. Declare both `server` and `client` roles.
3. If a role is unsupported, set `supported=False`, wire it to the placeholder adapter and give a precise `reason`. The matrix then has no row or column for that role.
4. Keep adapter summaries aligned with the common summary contract.
5. Update `adapters/README.md` if the adapter contract changes.
6. Verify both build-time and run-time behavior.

When adding a real adapter, make it consume harness CLI args rather than making the runner special-case that implementation.

## Editing guidance

- Prefer changing harness behavior in one place over duplicating logic across runner, adapters, and site.
- Preserve explicit naming: `server_impl`, `client_impl`, `initiator_role`.
- Keep result files machine-readable and stable; downstream checks depend on them.
- Treat `results/` as generated output, not source.
- If you change summary fields or artifact paths, update both the site generator and any runner expectations in the same change.

## Verification checklist

After meaningful changes, run the smallest relevant subset plus a syntax/build pass.

Typical minimum:

1. `python -m compileall src scripts`
2. `python -m unittest discover -s tests`
3. `python -m conformance.cli run --results-dir results --from aiosendspin --to aiosendspin,sendspin-dotnet --timeout-seconds 25`
4. `python -m conformance.cli report --results-dir results`

`tests/` holds stdlib `unittest` coverage for harness rules that the matrix
cannot reach on a machine without every toolchain installed. It needs no test
dependency beyond the package itself and runs in `ci.yml`. A test that needs an
implementation clone or a macOS toolchain skips itself when that is missing.

If you changed CI, read `.github/workflows/publish.yml` afterward and confirm:

- Pages still uploads from `artifacts/results`
- expected harness failures do not fail the workflow
- regression detection only fails PRs, not main/schedule/dispatch
- the macOS job still runs the full published matrix
- the published artifact still preserves `build-report.json` and `repositories.json`

## Git hygiene

- Make focused commits.
- Push regularly when changes are meaningful.
- Do not rewrite history unless explicitly asked.
