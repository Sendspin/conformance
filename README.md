# Sendspin Conformance

Conformance tooling for Sendspin implementations. It has two tools:

- `sendspin-validate` checks your own Sendspin client app against the spec.
- The `conformance` harness runs every tracked server against every tracked client
  and publishes the results as a matrix.

The latest matrix is live at <https://sendspin.github.io/conformance/>.

## Validate your client app

The only prerequisite is [uv](https://docs.astral.sh/uv/):

```sh
uvx --from git+https://github.com/Sendspin/conformance sendspin-validate
```

The validator is a strict Sendspin server. Your app either connects to it, or it
connects to your app, and it checks every frame your app sends. It then walks you
through the unpaired, pairing and post-pairing suites, prompting you at each step.

Useful flags:

- `--suite unpaired,pairing,post-pairing`: run only the named suites (default: all)
- `--connect ws://host:port/path`: connect to your app at this URL instead of using mDNS
- `--verbose`: stream every decoded frame to stderr
- `--fresh`: start from a new validator identity

The run writes `report.html`, `report.json` and `protocol.log.jsonl` to
`./sendspin-validate-<client-name>-<timestamp>/`. The exit status is 1 when any check
is `FAIL` or `TOOL_ERROR`.

## What the report shows

The report has one matrix per scenario. Rows are servers, columns are clients, and
each cell is one server→client case:

- `passed`: the case ran and the evidence matched what the scenario requires.
- `failed`: the case ran and something was wrong, such as a protocol violation, a
  mismatched hash, a connection error or a crashed adapter.
- `unsupported`: the implementation does not support this scenario or role, so the
  case is not a test of it.
- `unverifiable`: the case ran without an observed failure, but an SDK could not
  expose the evidence needed to judge it, or resampling ruled out comparing against
  the source PCM hash.

Each scenario, case and implementation has its own page with the logs and artifacts
behind the verdict. The scenario list lives in
[`src/conformance/scenarios.py`](src/conformance/scenarios.py) and per-implementation
coverage in [`src/conformance/implementations.py`](src/conformance/implementations.py).

## Run the harness locally

Prerequisites:

- [uv](https://docs.astral.sh/uv/), Python 3.12 or newer, and `git` (required)
- one toolchain per adapter you want built: `cargo` (sendspin-rs), `dotnet`
  (sendspin-dotnet), `swift` on macOS (SendspinKit), `cmake` and a C++ compiler
  (sendspin-cpp, sendspin-server-cpp), `node` and `npm` (sendspin-js)

A missing toolchain does not stop `scripts/run_all.py`: that adapter's build is
reported as failed, and its cases fail with the reason.

```bash
python scripts/setup_workspace.py --clone
. .venv/bin/activate
python scripts/run_all.py
```

This clones the implementation repositories, installs the harness, builds the
adapters, runs the matrix and writes the report to `results/index.html`.

Common commands:

```bash
conformance run --from aiosendspin,sendspin-rs --to SendspinKit   # a subset of the matrix
conformance run --jobs 4                                           # parallel cases
conformance build --report-path artifacts/build-report.json        # build adapters only
conformance report --results-dir results                           # regenerate the report
```

To use a checkout somewhere else, set `CONFORMANCE_REPO_<NAME>` to its absolute path,
with the repository name upper-cased and `-` written as `_` (for example
`CONFORMANCE_REPO_SENDSPIN_CLI`). A non-empty value must point at an existing path, or
the harness stops at startup.

## More detail

See [`AGENTS.md`](AGENTS.md) for harness internals, the scenario descriptions and
current coverage, and [`adapters/README.md`](adapters/README.md) for the adapter CLI
contract.
