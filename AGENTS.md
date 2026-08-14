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

- `README.md`: current user-facing workflow
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

Client-side unsupported roles still need a CLI surface that the harness can invoke. Those cases should fail fast, emit a summary, and exit non-zero. Server-side unsupported scenarios are filtered out before case creation, so the runner does not create dead server rows in the matrix.

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

Do not reintroduce SPA-style routing or inline all case details onto scenario pages.

### Report behavior

The site is static HTML, not a SPA. The index page shows all scenario matrices. Each scenario gets its own HTML page, and each concrete server/client pairing gets its own dedicated HTML page. Use only minimal vanilla JS; today that is limited to the case-page tabs in `src/conformance/site.py`.

Keep server/client roles explicit everywhere in the report. Do not regress back to ambiguous `from/to` terminology.

The overview page also shows repository revision metadata gathered at run time.
If you change how repositories are resolved, merged, or reported, update both
`src/conformance/repository_versions.py` and the overview rendering in
`src/conformance/site.py`.

### Summary contract

Successful adapters should print a JSON summary and exit `0`.

Important fields to preserve where available:

- `status`
- `implementation`
- `role`
- `scenario_id`
- `initiator_role`
- `preferred_codec`
- `peer_hello`
- audio hash fields

`peer_hello` should contain the full hello message received from the other party whenever capture is possible.

### Audio fixture

The shared source fixture is `almost_silent.flac` from `sendspin-cli`:

- resolved by `src/conformance/fixtures.py`
- expected at `sendspin-cli/tests/fixtures/almost_silent.flac`

Do not hard-code another fixture path in adapters or tests.

### CI expectation

GitHub Actions should still publish artifacts and GitHub Pages even when the conformance matrix has failing cases. Harness failures are currently expected. Do not add a workflow step that turns expected red matrix cases into a failed workflow.

Build/setup failures are different: those can still be real workflow failures.

The published workflow currently runs on `macos-latest` and publishes one matrix without host-specific sections in the UI. Internal environment metadata may still exist in raw artifacts for build-log lookup or local merged runs, but the public report should read as one authoritative matrix.

### Regression detection

After building the report, CI compares local results against the live published baseline at `https://sendspin.github.io/conformance/data/index.json`. A *regression* is any case that was `passed` in the baseline but is no longer `passed` locally.

- **Pull requests**: the workflow fails if regressions are detected.
- **Pushes to main / scheduled / manual**: regressions are logged and a Discord notification text is uploaded as an artifact (`discord-regression-notice`), but the workflow does not fail so that Pages still publishes.

If the baseline is unreachable (first publish, network error), the check is skipped silently.

## Repository map

### Harness code

- `src/conformance/models.py`: shared dataclasses and scenario capability helpers
- `src/conformance/scenarios.py`: scenario registry and order
- `src/conformance/implementations.py`: implementation registry and role metadata
- `src/conformance/runner.py`: matrix execution and process orchestration
- `src/conformance/build.py`: adapter build checks
- `src/conformance/repository_versions.py`: git revision metadata written into `results/data/repositories.json`
- `src/conformance/declared_formats.py`: joins client format declarations to the formats the matrix negotiated, written into `results/data/declared-formats.json`
- `src/conformance/merge.py`: merges host-specific raw result directories, including build and repository metadata
- `src/conformance/site.py`: static site generation
- `src/conformance/flac.py` and `src/conformance/pcm.py`: canonical decode/hash helpers

### Adapters

- `src/conformance/adapters/aiosendspin_server.py`: real Python server adapter
- `src/conformance/adapters/aiosendspin_client.py`: real Python client adapter
- `adapters/sendspin-dotnet/client/`: real `.NET` client adapter source
- `adapters/sendspin-go/`: real Go client/server adapter source
- `adapters/SendspinKit/client/`: real Swift client adapter source with an adapter-owned inbound WebSocket listener for server-initiated scenarios
- `adapters/sendspin-rs/client/`: real Rust client adapter source
- `adapters/sendspin-js/client.mjs`: real Node.js client adapter source that drives the public `SendspinCore` SDK over an adapter-owned WebSocket
- `src/conformance/adapters/placeholder.py`: generic fail-fast adapter

### Scripts

- `scripts/setup_repositories.py`: clone/check required repos
- `scripts/setup_workspace.py`: create local venv and install Python deps
- `scripts/run_all.py`: build + run + report orchestration
- `scripts/merge_results.py`: merge multiple host result sets into one report

### CI

- `.github/workflows/publish.yml`: macOS build + run + Pages publishing
- `scripts/detect_regressions.py`: compares local results against the published baseline to detect regressions

## Standard commands

Bootstrap locally:

```bash
python scripts/setup_workspace.py --clone
. .venv/bin/activate
```

Build adapters:

```bash
python -m conformance.cli build
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
2. Expose both `server` and `client` roles.
3. If a role is unsupported, wire it to a fail-fast adapter and give a precise `reason`.
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
2. `python -m conformance.cli run --results-dir results --from aiosendspin --to aiosendspin,sendspin-dotnet --timeout-seconds 25`
3. `python -m conformance.cli report --results-dir results`

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
