# Sendspin Conformance

Capability-aware conformance harness for local Sendspin implementations.

Current scenarios:

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

## Current coverage

- `aiosendspin`: real server adapter, including OPUS encoding, and real client adapter; the client SDK decodes PCM and FLAC only, so it does not advertise OPUS
- `sendspin-dotnet`: real client adapter for client-initiated PCM plus the server-initiated PCM, metadata, artwork, controller, and FLAC scenarios; server placeholder
- `SendspinKit`: real Swift client adapter for client-initiated PCM, Flac, and Opus plus the server-initiated PCM, Flac, Opus, metadata, artwork, and controller scenarios; server placeholer
- `sendspin-cpp`: real C++ client adapter for client-initiated PCM plus the server-initiated PCM, 24-bit PCM, metadata, artwork, controller, FLAC, OPUS, legacy-unencrypted, protocol-baseline and audio-chunk-framing scenarios, evidenced from the library's message trace; it joins the `client-initiated-state-format-*` scenarios as a player but fails them, since the library exposes no way to set the `client/state` player `format`; server placeholder
- `sendspin-go`: not currently tested. Both roles are placeholders: the server library has no RC1 `client/init` or Noise handshake, and the Go client adapter source in `adapters/sendspin-go/` is kept but no longer built or run
- `sendspin-js`: real Node.js client adapter driving the public `SendspinCore` SDK over an adapter-owned WebSocket; covers client-initiated PCM plus the server-initiated PCM, metadata, and controller scenarios (FLAC/OPUS decode and `artwork@v1` are not exposed by the public SDK in a headless Node environment); server placeholder
- `sendspin-rs`: real Rust client adapter for client-initiated PCM plus the server-initiated PCM, metadata, artwork, controller, and FLAC scenarios; server placeholder
- `sendspin-server-cpp`: real C++ server adapter for the client- and server-initiated PCM, 24-bit PCM, protocol-baseline, audio-chunk-framing, metadata, artwork and controller scenarios, the server-initiated FLAC and OPUS scenarios (both encoded by the library) and both `client-initiated-state-format-*` scenarios, built on the library's public API and evidenced from its message trace; the library has no unencrypted mode, so it takes no part in `server-initiated-legacy-unencrypted`; client placeholder (its client counterpart is `sendspin-cpp`)

The OPUS scenario is currently exercised only by the `SendspinKit` and `sendspin-cpp` clients against the `aiosendspin` and `sendspin-server-cpp` servers until other implementations opt in via `supports_opus`.

The `client-initiated-state-format-*` scenarios have no capability flag of their own: RC1 gives every player the `client/state` `format` preference, so a client that never sends one shows as a failed case. The FLAC variant still needs a client that can list FLAC (`supports_flac`). The `aiosendspin` and `SendspinKit` client adapters drive them today.

The `client-initiated-metadata`, `client-initiated-artwork` and `client-initiated-controller` scenarios have no capability flag of their own either: RC1 requires every server to support client-initiated connections, whichever roles the client brings. The `aiosendspin`, `sendspin-dotnet`, `SendspinKit` and `sendspin-cpp` client adapters drive all three over the outbound path they use for `client-initiated-pcm`. The `sendspin-js` client adapter drives the metadata and controller scenarios the same way; it declares no `artwork` role family, so the harness marks its `client-initiated-artwork` cases unsupported before launching anything. The `sendspin-rs` client adapter has an outbound path for player sessions only, so its cases fail as a harness gap rather than being left out.

The `server-initiated-audio-chunk-framing` scenario has no capability flag of its own either. A client adapter that is handed parsed chunks and never sees a raw frame reports that, and its case fails as a harness gap rather than being left out. Only the `aiosendspin` adapters, the `sendspin-server-cpp` server and the `sendspin-rs` and `sendspin-cpp` clients observe raw frames today.

An implementation without a client role gets no client column, and one without a server role gets no server row. Server rows are also left out per scenario, so the matrix only shows server rows that can actually run it. A client that implements the role but cannot run one scenario keeps its column: its case fails fast through a placeholder adapter and reads as unsupported.

## Protocol conformance migration

The matrix is moving from media-oriented interoperability checks to normative protocol
conformance. `server-initiated-protocol-baseline-v1` is the first authoritative
protocol test and is pinned to Sendspin spec revision
`8c9577ea8719ad082d051ec13cc73ef15ed68948`. That pin scopes this scenario's assertions
only. It is not the revision the matrix is audited against: the verdicts applied to every
case are judged against the spec checkout each run records in `repositories.json` and
shows on the report overview. The scenario intentionally fails until an adapter
can provide the required ordered protocol evidence described in
[`adapters/README.md`](adapters/README.md). The current PCM, FLAC, OPUS, artwork,
metadata, and controller scenarios remain diagnostic interoperability checks during
this migration: a matching hash or normalized snapshot is evidence that two adapters
interoperated, not proof that either followed the specification.

## Quick start

```bash
python scripts/setup_workspace.py --clone
. .venv/bin/activate
python scripts/run_all.py
```

That flow:

- clones the required repositories
- installs the Python harness and `aiosendspin`
- builds the adapter sources that are available locally
- runs the current matrix for the selected host environment
- generates the static HTML report

To run against a checkout somewhere else, set `CONFORMANCE_REPO_<NAME>` to its
absolute path, with the repository name upper-cased and `-` written as `_` (for
example `CONFORMANCE_REPO_SENDSPIN_CLI`). A non-empty value must point at an
existing path: the harness stops at startup rather than fall back to `repos/<name>`.
An empty value counts as unset.

## Useful commands

Run the full harness:

```bash
python scripts/run_all.py --results-dir results --build-report-path artifacts/build-report.json
```

Run the full harness for an explicit host label:

```bash
python scripts/run_all.py \
  --results-dir results \
  --build-report-path artifacts/build-report.json \
  --environment-id linux \
  --environment-name Linux
```

Run a subset of the matrix:

```bash
conformance run --from aiosendspin,sendspin-rs --to SendspinKit
```

Run the matrix with parallel case execution:

```bash
conformance run --jobs 4
python scripts/run_all.py --jobs 4
```

The runner assigns a dedicated server port and client-listener port to each case,
so parallel cases do not fight over `8927`/`8928`.

Build the adapter sources only:

```bash
conformance build --report-path artifacts/build-report.json
```

Generate the static site from existing results:

```bash
conformance report --results-dir results
```

Merge multiple host result sets into one combined report:

```bash
python scripts/merge_results.py \
  --output-dir artifacts/results \
  artifacts/linux-results \
  artifacts/macos-results
```

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

Every step waits for you, and every question accepts `s` (skip), `r` (retry) and `?`
(not sure). The hidden `--auto-answer` flag is for the CI self-test only.

The run writes `report.json`, a self-contained `report.html` and `protocol.log.jsonl`
to `./sendspin-validate-<client-name>-<timestamp>/`. Pairing secrets are redacted as
frames are recorded, and so is every code you enter. The exit status is 1 when any
check is `FAIL` or `TOOL_ERROR`.

## Report site

The generated site includes:

- a global matrix overview with one section per test scenario
- a single matrix view per scenario without host-specific grouping in the UI
- the greener PCM scenarios listed first on the index page
- a separate static HTML page per test under `results/scenarios/`
- a dedicated static HTML page per pairing under `results/cases/`
- a dedicated static HTML page per implementation under `results/implementations/` for linkable filtered overviews
- the audio formats each client declared in `client/hello`, marked exercised or not verified, on that client's implementation page
- per-case status and reason with explicit server/client labeling
- summary, server, client, and build tabs on each dedicated case page when build data exists
- run artifacts under `results/data/`, including `declared-formats.json` for the declared-versus-exercised data
- the static report at `results/index.html`
- linked case artifacts for drill-down: `result.json`, client/server summaries, and logs

## Repository layout

- `src/conformance/`: runner, adapters, fixture decoding, report generation
- `src/conformance/validator/`: the `sendspin-validate` client validator
- `src/conformance/_vendor/` and `patches/aiosendspin/`: the aiosendspin the validator bundles, generated by `scripts/vendor_aiosendspin.py`
- `adapters/sendspin-go/`: Go client adapter source, kept but not currently built or tested
- `adapters/sendspin-server-cpp/server/`: C++ server adapter source
- `adapters/sendspin-dotnet/`: `.NET` client adapter source
- `adapters/README.md`: CLI contract for adapters
- `scripts/setup_repositories.py`: clones implementation repositories
- `scripts/setup_workspace.py`: bootstraps a local Python environment
- `scripts/run_all.py`: build + run + report orchestration
- `scripts/merge_results.py`: merges multiple host result directories into one report
- `.github/workflows/publish.yml`: macOS GitHub Pages publishing
- `.github/workflows/ci.yml`: Linux unit tests, including the validator self-test
