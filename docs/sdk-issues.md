# SDK Issues to File

Per-SDK punch list extracted from the seven audit docs in this directory.
Each bullet here is a candidate GitHub issue against the named SDK's repo.
Spec-text recommendations and missing conformance scenarios are tracked
separately and are **not** included here — this list is only concrete,
SDK-side code work.

Severity hint:
- **🔴 critical** — audible misbehavior or direct spec violation that
  affects normal playback today
- **🟠 high** — wrong defaults, parse-but-ignore, or missing behavior the
  rest of the SDK already assumes
- **🟡 medium** — missing API surface or divergence from spec where the
  practical impact is bounded
- **⚪ latent** — code path that would break in a scenario not exercised
  today (24-bit PCM, FLAC mid-stream, etc.)

Source docs:
[stream-sync-correction.md](./stream-sync-correction.md),
[clock-synchronization.md](./clock-synchronization.md),
[codec-format-negotiation.md](./codec-format-negotiation.md),
[goodbye-and-operational-state.md](./goodbye-and-operational-state.md),
[reconnection-and-multi-server.md](./reconnection-and-multi-server.md),
[static-delay.md](./static-delay.md),
[volume-curve.md](./volume-curve.md).

---

## `Sendspin/aiosendspin`

Transport+clock library. Most "missing" behavior is by design (the
embedder owns playback). No code bugs identified — see `sendspin-cli`
for the Python embedder's followups.

One potential SDK-shaped followup, not strictly a bug:

- ⚪ **Expose a hook for `state: 'error'` / `synchronized` underrun
  reporting.** The library is the only place that knows when the
  Kalman filter / chunk pipeline can't keep up; today there is no
  callback for the embedder to drive the spec's underrun handshake.
  Source: stream-sync-correction.md §`aiosendspin`.

---

## `Sendspin/sendspin-cli`

- 🟠 **Web UI sends `shutdown` on tab close instead of `restart`.**
  `destroyPlayer()` defaults to `shutdown`; only the explicit
  user-stop button sends `user_request`. A user who closes the
  browser tab is reported to the server as "shut down permanently"
  and is not auto-reconnected.
  Source: goodbye-and-operational-state.md §divergence.
- 🟡 **Underrun does not send `client/state: 'error'`.** The
  sounddevice callback at `sendspin/audio.py:432-437` detects
  underrun and triggers a queue clear, but `send_player_state()`
  (`audio_connector.py:456-460`) always sends `SYNCHRONIZED`.
  Source: stream-sync-correction.md §`sendspin-cli`.
- ⚪ **Range coercion is silent.** Negative `static_delay_ms` is
  coerced positive without a warning; out-of-range from a misbehaving
  server is accepted. Either reject explicitly or log on coercion.
  Source: static-delay.md §divergence.

---

## `Sendspin/sendspin-cpp`

Reference SDK for most behaviors. One spec-rule gap:

- 🟡 **Underrun does not send `client/state: 'error'`.** Detected at
  `src/sync_task.cpp:683-687` and logged, but never converted to the
  protocol message. `ERROR` exists in the enum but is unused.
  Source: stream-sync-correction.md §`sendspin-cpp`.

---

## `Sendspin/sendspin-dotnet`

- 🟠 **Default goodbye reason is `user_request` (factory default).**
  Should be `restart` so an unexplained graceful disconnect is
  reconnectable; today the default disables server-side
  auto-reconnect. Source: goodbye-and-operational-state.md
  §divergence.
- 🟠 **`static_delay_ms` parsed but no playback wire-up visible.**
  Field is parsed on `ClientStateMessage`; the audit could not find
  the path that subtracts it before scheduling. Either it is missing
  or it is hidden — needs a targeted audit and, if missing, a fix.
  Source: static-delay.md §per-implementation summary.
- 🟠 **No `static_delay_ms` persistence across restarts.** Spec
  requires the client to persist; today the embedder must re-supply
  on every connect. Add an SDK-level persistence hook.
  Source: static-delay.md §divergence.
- 🟠 **Internal `ErrorOccurred` / `ReanchorRequired` events not wired
  to `client/state: 'error'`.** The plumbing exists; nothing
  subscribes those events to emit the spec's underrun handshake.
  Source: stream-sync-correction.md §`sendspin-dotnet`.
- 🟡 **Audit `volume` conversion.** Field is parsed in
  `ClientStateMessage` but the audio pipeline is not reachable from
  the public API in the audited version. If the curve isn't applied,
  add `(vol/100)^1.5`. Source: volume-curve.md §divergence.
- 🟡 **No last-played-server persistence / no multi-server
  arbitration / no auto `another_server` on switch.** Today the
  decision collapses to "whichever connection happened first" and a
  switch sends the wrong (or no) goodbye reason. Source:
  reconnection-and-multi-server.md §divergence.
- 🟡 **No `external_source` API.** The state value parses but no
  embedder-facing handle exists to enter/leave external-source.
  Source: goodbye-and-operational-state.md §divergence.
- ⚪ **FLAC `codec_header` decode path not visible.** Field parsed on
  the struct; audit could not find Base64 decode → micro-flac feed.
  If absent, FLAC streams cannot start. Source:
  codec-format-negotiation.md §divergence.
- ⚪ **No delta `client/state` updates.** Always sends full payload;
  no test coverage for partial-update merge. Source:
  goodbye-and-operational-state.md §divergence.
- ⚪ **No `stream/request-format` ever emitted.** Same gap across all
  SDKs — listed here for completeness. Source:
  codec-format-negotiation.md §divergence.

---

## `Sendspin/sendspin-go`

- 🔴 **Linear volume gain (`vol/100`).** `pkg/sendspin/scheduler.go`
  multiplies the raw ratio into the buffer; at volume 50 outputs
  0.5 amplitude where a conformant player outputs ≈0.354. A
  multi-room group containing one Go client will be audibly louder
  than conformant peers. Fix: `math.Pow(v/100, 1.5)`. Source:
  volume-curve.md §divergence.
- 🔴 **Server ignores `client/goodbye.reason`.**
  `pkg/sendspin/server_dispatch.go:118-131` logs the reason but does
  not set the retry flag. A client sending `restart` is not
  auto-reconnected; a client sending `shutdown` is. One-line fix
  mirroring aiosendspin's `connection.py:712`. Source:
  reconnection-and-multi-server.md §divergence.
- 🔴 **Server does not handle `external_source` state.** No group
  swap / `group/update` / `stream/end` on the incoming state change.
  Match aiosendspin's `server/roles/controller/v1.py:93-119`. Source:
  goodbye-and-operational-state.md §reference implementations.
- 🟠 **No 24-bit PCM sign-extension path.** Audio processing uses
  `binary.LittleEndian` correctly for 16/32-bit only. A negotiated
  24-bit stream would be misread. Add a 24-bit-to-32-bit unpack with
  sign-extension. Source: codec-format-negotiation.md §divergence.
- 🟡 **No drift correction in the SDK** (Kalman timestamp math only).
  Whether to do this in the SDK or leave to the embedder is a design
  call; flagging because peers like `sendspin-rs` do it in the SDK.
  Source: stream-sync-correction.md §`sendspin-go`.
- 🟡 **No last-played-server persistence / no multi-server
  arbitration / no auto `another_server` on switch / no reconnect
  backoff.** Source: reconnection-and-multi-server.md §divergence.
- 🟡 **No `static_delay_ms` persistence across restarts.** Source:
  static-delay.md §divergence.
- 🟡 **No `external_source` client API.** Source:
  goodbye-and-operational-state.md §divergence.
- ⚪ **No `static_delay_ms` range validation (0–5000).** Source:
  static-delay.md §summary.
- ⚪ **FLAC `codec_header` decode path not visible.** Source:
  codec-format-negotiation.md §divergence.
- ⚪ **No delta `client/state` updates.** Source:
  goodbye-and-operational-state.md §divergence.
- ⚪ **No `stream/request-format` ever emitted.** Source:
  codec-format-negotiation.md §divergence.

---

## `Sendspin/sendspin-js`

- 🔴 **Linear volume gain on `GainNode`.** `src/audio/scheduler.ts:615`
  sets `gainNode.gain = stateManager.volume / 100`. Should apply
  `Math.pow(v/100, 1.5)` (ideally via a smoothed ramp) before
  assignment. Source: volume-curve.md §divergence.
- 🟠 **Default goodbye reason is `shutdown`.** Should be `restart` so
  an unexplained close is reconnectable. Source:
  goodbye-and-operational-state.md §divergence.
- 🟠 **`PlayerState = "error"` defined but never emitted.** Underrun
  paths never transition into it. Wire the scheduler's underrun
  detection to send `client/state: 'error'` → mute → buffer →
  `synchronized`. Source: stream-sync-correction.md §`sendspin-js`.
- 🟡 **PCM-first codec ordering.** `codec-support.ts:44-76` advertises
  `[pcm, opus, flac]`, causing servers to choose uncompressed by
  default and waste bandwidth. Reorder to put compressed codecs
  first (subject to existing per-browser filtering). Source:
  codec-format-negotiation.md §divergence.
- 🟡 **Bootstrap gate too loose.** Filter accepts after a single
  measurement, combined with the most aggressive (2.0) adaptive
  cutoff in the survey. Raise to `count ≥ 2 + finite covariance`.
  Source: clock-synchronization.md §divergence.
- 🟡 **No last-played-server persistence / no multi-server
  arbitration / no auto `another_server` on switch.** Source:
  reconnection-and-multi-server.md §divergence.
- 🟡 **No `static_delay_ms` persistence across restarts.** Source:
  static-delay.md §divergence.
- 🟡 **No `external_source` API.** Source:
  goodbye-and-operational-state.md §divergence.
- ⚪ **24-bit PCM unsupported by Web Audio AudioContext.** Not a bug
  per se, but the advertised formats should explicitly filter out
  `bit_depth: 24` for PCM to prevent any 24-bit negotiation outcome.
  Source: codec-format-negotiation.md §divergence.
- ⚪ **No delta `client/state` updates.** Source:
  goodbye-and-operational-state.md §divergence.
- ⚪ **No `stream/request-format` ever emitted.** Source:
  codec-format-negotiation.md §divergence.

---

## `Sendspin/sendspin-jvm`

- 🟠 **`PlayerStatePayload.state` is hardcoded `"synchronized"`.**
  Underrun is exposed via a `StateFlow` for the embedder, but no
  protocol-level error state is ever sent. Plumb the flow into the
  outgoing `client/state` message. Source:
  stream-sync-correction.md §`sendspin-jvm`.
- 🟠 **Audit `volume` conversion.** Currently delegated to Android
  audio APIs; `AudioTrack.setVolume()` is documented linear, so
  delegation is likely silently non-conformant. Apply
  `(vol/100)^1.5` in the SDK. Source: volume-curve.md §divergence.
- 🟠 **`static_delay_ms` not applied in playback** (no pipeline
  visible) and not persisted. Source: static-delay.md
  §per-implementation summary.
- 🟡 **Drift correction missing in SDK** (Kalman timestamp math
  only). Flagged like sendspin-go: design call. Source:
  stream-sync-correction.md §`sendspin-jvm`.
- 🟡 **Late-chunk threshold is 1 s** — deliberately loose for Music
  Assistant seek bursts. Worth documenting the rationale and
  considering a configurable knob. Source: stream-sync-correction.md
  §`sendspin-jvm`.
- 🟡 **Multi-server arbitration is only partial** (server-host
  helper auto-sends `another_server` during negotiation). No
  last-played persistence in the SDK proper. Source:
  reconnection-and-multi-server.md §divergence.
- 🟡 **No `external_source` API.** Source:
  goodbye-and-operational-state.md §divergence.
- ⚪ **FLAC `codec_header` decode path not visible.** Source:
  codec-format-negotiation.md §divergence.
- ⚪ **No delta `client/state` updates.** Source:
  goodbye-and-operational-state.md §divergence.
- ⚪ **No `stream/request-format` ever emitted.** Source:
  codec-format-negotiation.md §divergence.

---

## `Sendspin/sendspin-rs`

- 🔴 **`static_delay_ms` parsed but never applied in playback.**
  Flagged in static-delay.md as the **highest-severity bug found in
  the audit**: the server factors the delay into when it sends
  audio, but the client scheduler ignores it, so alignment is off
  by exactly the delay amount. Thread the value from the protocol
  handler through `SyncedPlayer` and subtract where the
  server→local conversion happens (`src/sync/clock.rs`). Source:
  static-delay.md §recommendations.
- 🟠 **`ClientSyncState::Error` defined but never sent.**
  `src/protocol/messages.rs:285` defines the variant; underrun in
  the audio callback emits zeros and freezes the cursor but never
  transitions state. Source: stream-sync-correction.md
  §`sendspin-rs`.
- 🟠 **Kalman drift applied unconditionally (no SNR gate).** When
  the filter has just seen its first few measurements, noise-
  dominated drift propagates wrong timestamps into the scheduler.
  Add `drift² ≥ k² × drift_covariance` (k ≥ 2) gate; below
  threshold, fall back to offset-only. Source:
  clock-synchronization.md §divergence.
- 🟡 **No last-played-server persistence / no multi-server
  arbitration / no auto `another_server` on switch / no reconnect
  backoff.** Source: reconnection-and-multi-server.md §divergence.
- 🟡 **No `external_source` API.** Source:
  goodbye-and-operational-state.md §divergence.
- ⚪ **No delta `client/state` updates.** Source:
  goodbye-and-operational-state.md §divergence.
- ⚪ **No `stream/request-format` ever emitted.** Source:
  codec-format-negotiation.md §divergence.

---

## `Sendspin/SendspinKit`

- 🔴 **`static_delay_ms` parsed but never applied in playback.** Same
  shape as the sendspin-rs bug — server pre-compensates for the
  delay, client ignores it, alignment is wrong by exactly that
  amount. Subtract in `AudioScheduler` where server timestamps are
  converted, alongside the existing parsing in `SendspinClient`.
  Source: static-delay.md §recommendations.
- 🟠 **Default goodbye reason is `shutdown`.** Should be `restart`.
  Source: goodbye-and-operational-state.md §divergence.
- 🟠 **Underrun does not send `client/state: 'error'`.** Error state
  *is* sent on codec/format failures
  (`SendspinClient+MessageHandling.swift:224-241`), but the audio
  callback at `AudioPlayer.swift:558` only increments a counter on
  underrun. Wire the underrun path to
  `transitionOperationalState(to: .error)`. Source:
  stream-sync-correction.md §`SendspinKit`.
- 🟡 **Bootstrap gate too loose (1 sample).** Raise to `count ≥ 2 +
  finite covariance`. Source: clock-synchronization.md §divergence.
- 🟡 **No last-played-server persistence / no multi-server
  arbitration / no auto `another_server` on switch / no reconnect
  backoff visible.** Source: reconnection-and-multi-server.md
  §divergence.
- ⚪ **No delta `client/state` updates.** Source:
  goodbye-and-operational-state.md §divergence.
- ⚪ **No `stream/request-format` ever emitted.** Source:
  codec-format-negotiation.md §divergence.

---

## Cross-cutting items (not for filing as SDK issues)

These are findings the audit raised that don't belong as per-SDK
issues. Listed here so they aren't forgotten:

- **Spec text changes** — recommended threshold values, drift SNR
  gate as MUST, default goodbye reason, perceptual volume curve,
  late-chunk drop threshold, etc. File against `Sendspin/spec`.
- **Conformance harness scenarios** — drift injection, static-delay
  measurement, volume calibration, multi-server arbitration,
  `external_source` group swap, `stream/request-format` negotiation,
  24-bit PCM fixture. File against `Sendspin/conformance`.
- **Universal SDK gaps** — these missed-by-everyone items are
  better tackled by a spec change first, then propagated:
  - No SDK emits `stream/request-format`.
  - No SDK sends delta `client/state` updates.
  - No client SDK except `SendspinKit` exposes `external_source`.
  - No SDK sends the spec's underrun `error → synchronized`
    handshake (every per-SDK list above includes its own variant).
