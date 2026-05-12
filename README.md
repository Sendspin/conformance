# Sendspin Conformance

Capability-aware conformance harness for local Sendspin implementations.

Current scenarios:

- `client-initiated-pcm` (Client initiates connection and client wants PCM): start the server first, let the client discover/connect to it, advertise PCM as the only supported audio format, and compare canonical PCM hashes
- `server-initiated-pcm` (Server initiates connection and client wants PCM): start the server first, let the client advertise a listener and PCM as its only supported audio format, let the server connect in, and compare canonical PCM hashes
- `server-initiated-metadata` (Server initiates connection and client wants Metadata): start the server first, let the client advertise a listener, let the server connect in, receive a metadata snapshot, and compare normalized metadata fields
- `server-initiated-artwork` (Server initiates connection and client wants Artwork): start the server first, let the client advertise a listener, let the server connect in, receive album artwork bytes, and compare the encoded image hash
- `server-initiated-controller` (Server initiates connection and client wants Controller): start the server first, let the client advertise a listener, let the server connect in, observe controller state, send a control command, and verify the server recorded it
- `server-initiated-flac` (Server initiates connection and client wants FLAC): start the server first with PCM audio decoded from `almost_silent.flac`, let the client advertise a listener and FLAC as its only supported audio format, let the server connect in, encode the PCM to FLAC using the SDK, stream it to the client, and compare the transported FLAC bytes
- `server-initiated-opus` (Server initiates connection and client wants OPUS): start the server first with PCM audio decoded from `almost_silent.flac`, let the client advertise a listener and OPUS as its only supported audio format, let the server connect in, encode the PCM to OPUS using the SDK, stream it to the client, and compare the transported OPUS bytes

## Current coverage

- `aiosendspin`: real server adapter and real client adapter, including OPUS support
- `sendspin-dotnet`: real client adapter for client-initiated PCM plus the server-initiated PCM, metadata, artwork, controller, and FLAC scenarios; server placeholder
- `SendspinKit`: client intentionally unsupported until conformance can use the public SDK like an example application, without bespoke protocol code; server placeholder
- `sendspin-cpp`: real C++ client adapter for client-initiated PCM plus the server-initiated PCM, metadata, artwork, controller, and FLAC scenarios; server placeholder
- `sendspin-go`: real Go client adapter and real Go server adapter across the PCM, FLAC, metadata, artwork, and controller scenarios (no OPUS yet)
- `sendspin-js`: real Node.js client adapter driving the public `SendspinCore` SDK over an adapter-owned WebSocket; covers client-initiated PCM plus the server-initiated PCM, metadata, and controller scenarios (FLAC/OPUS decode and `artwork@v1` are not exposed by the public SDK in a headless Node environment); server placeholder
- `sendspin-jvm`: real JVM client adapter written in Kotlin, for client-initiated PCM plus the server-initiated PCM, metadata, artwork, controller, and FLAC scenarios; server placeholder
- `sendspin-rs`: real Rust client adapter for client-initiated PCM plus the server-initiated PCM, metadata, artwork, controller, and FLAC scenarios; server placeholder

The OPUS scenario is currently exercised only by the `aiosendspin` server and client until other implementations opt in via `supports_opus`.

Unsupported client roles use fail-fast adapters that emit a summary and exit non-zero. Unsupported server roles are filtered out before case creation, so the matrix only shows server rows that can actually run a scenario.

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

## Report site

The generated site includes:

- a global matrix overview with one section per test scenario
- a single matrix view per scenario without host-specific grouping in the UI
- the greener PCM scenarios listed first on the index page
- a separate static HTML page per test under `results/scenarios/`
- a dedicated static HTML page per pairing under `results/cases/`
- a dedicated static HTML page per implementation under `results/implementations/` for linkable filtered overviews
- per-case status and reason with explicit server/client labeling
- summary, server, client, and build tabs on each dedicated case page when build data exists
- run artifacts under `results/data/`
- the static report at `results/index.html`
- linked case artifacts for drill-down: `result.json`, client/server summaries, and logs

## Repository layout

- `src/conformance/`: runner, adapters, fixture decoding, report generation
- `adapters/sendspin-go/`: Go client/server adapter source
- `adapters/sendspin-dotnet/`: `.NET` client adapter source
- `adapters/README.md`: CLI contract for adapters
- `scripts/setup_repositories.py`: clones implementation repositories
- `scripts/setup_workspace.py`: bootstraps a local Python environment
- `scripts/run_all.py`: build + run + report orchestration
- `scripts/merge_results.py`: merges multiple host result directories into one report
- `.github/workflows/publish.yml`: macOS GitHub Pages publishing
