# Adapter Contract

Every implementation in the matrix is modeled as two CLIs:

- `server`: starts first, either discovers/connects to a target client or advertises its own endpoint for a client-initiated scenario, drives the requested protocol interaction, writes a JSON summary, exits `0` on success
- `client`: starts second, either listens for a server-initiated connection or discovers/connects to a server for a client-initiated scenario, participates in the requested protocol interaction, writes a JSON summary, exits `0` on success

Current checked-in adapters:

- `src/conformance/adapters/aiosendspin_server.py`: real Python server adapter
- `src/conformance/adapters/aiosendspin_client.py`: real Python client adapter
- `adapters/sendspin-dotnet/client/`: real `.NET` client adapter source for client-initiated PCM plus the server-initiated PCM, metadata, artwork, controller, and FLAC scenarios
- `adapters/sendspin-go/`: real Go adapter source for the current client/server scenario set
- `adapters/SendspinKit/client/`: real Swift client adapter source for client-initiated PCM plus the server-initiated PCM, metadata, artwork, controller, and FLAC scenarios
- `adapters/sendspin-js/client.mjs`: real Node.js client adapter for client-initiated PCM plus the server-initiated PCM, metadata, and controller scenarios, driving the public `SendspinCore` SDK over an adapter-owned WebSocket
- `adapters/sendspin-rs/client/`: real Rust client adapter source for client-initiated PCM plus the server-initiated PCM, metadata, artwork, controller, and FLAC scenarios
- `src/conformance/adapters/placeholder.py`: fail-fast placeholder for unsupported roles

Current placeholders in the matrix are modeled in `src/conformance/implementations.py` and fail immediately with a summary explaining why the role is unavailable for a scenario.

## Initial activation

Every server adapter MUST report an `activation` field in its summary: the first
`server/activate` it sent, as it went on the wire, in the same `{"type": ..., "payload": ...}`
shape as `peer_hello`. Report `null` only when the server sent none. Never reconstruct it
from adapter arguments or SDK state.

## Metadata scenario summary fields

The spec requires the first `server/state` sent for a role on a connection to
carry a past or present `timestamp` when the role object has one, so the client
is current before any scheduled update follows. The matrix checks that from two
fields, both optional: a case where either side is absent is not judged, because
an unreadable claim is not evidence against an implementation.

- **client**, `metadata.first_object_state`: `{"update_index": N,
  "timestamp_us": T}` for the first `server/state` that carried a metadata
  object. A state whose object is an explicit null carries no timestamp and so
  is passed over; `update_index` is the 1-based position among the states the
  client observed, which makes any skipped ones visible. `timestamp_us` is the
  object's `timestamp` as received, or `null` if it carried none — report the
  key only if the adapter reads the field, since `null` is taken to mean the
  server omitted it.
- **server**, `metadata.first_state_sent`: `{"timestamp_us": T, "bound_us": B}`
  for the first `server/state` the server sent carrying a metadata object with a
  timestamp. `timestamp_us` is the timestamp as it went out on the wire, and
  `bound_us` is a reading of the server's own clock. Both are in the server's
  clock domain, so they need no relation to any other implementation's clock.

  **Take the clock reading after the state is on the wire, never before.** The
  reading has to be an upper limit on when that frame was really sent, and only
  a later reading is sound: a reading taken before transmission calls a
  timestamp future when it was already past by the time it went out, and so
  fails a conformant server. A reading taken later can only make the check more
  permissive. A send call that merely queues the message has not sent it, so
  reading the clock when it returns is too early.

  Do not block waiting for the queue to drain. Observe the write where it
  happens and report nothing when it was not seen: the matrix judges nothing
  rather than failing the case, which keeps the adapter inside the case budget
  if the client goes away.

The matrix requires both sides to name the same `timestamp_us` before applying
the bound, so the two are known to describe the same state. If they differ,
nothing is judged.

## Protocol evidence contract

Protocol scenarios are authoritative conformance tests, not audio-rendering tests. For
each requested protocol assertion, both adapters MUST add this evidence to their summary:

```json
{
  "protocol": {
    "spec_revision": "8c9577ea8719ad082d051ec13cc73ef15ed68948",
    "assertions": {
      "CORE-001": {
        "status": "passed",
        "events": [{"direction": "outbound", "message_type": "client/init"}]
      }
    }
  }
}
```

`events` is a compact, ordered protocol trace observed at the adapter/SDK boundary.
Record message direction, message type, transport/frame information, and only the
payload fields needed to establish the assertion. Do not record secrets, Noise keys,
PSKs, or decoded/rendered audio. An assertion that cannot be observed must be reported
as `failed` with a `detail` explaining the missing SDK hook; it must not be inferred
from successful playback.

The assertion identifiers and pinned specification revision are defined in
`src/conformance/protocol.py`. The initial `server-initiated-protocol-baseline-v1`
scenario exercises `CORE-001` through `CORE-004` and `PLAYER-001`. Existing
media-hash scenarios remain interoperability diagnostics during the migration; their
pass status does not establish protocol conformance.

## Format preference contract

The `client-initiated-state-format-*` scenarios judge the `format` field of the
`client/state` player object, so the evidence has to show which message carried the
preference. Both adapters add a block to their summary:

```json
{
  "format_preference": {
    "received": {"codec": "pcm", "sample_rate": 8000, "channels": 1, "bit_depth": 16}
  }
}
```

The server records the last `format` it received in a `client/state` player object,
or `null` when none arrived. A format requested through any other message must not be
recorded here.

```json
{
  "renegotiation": {
    "requested": {"codec": "pcm", "sample_rate": 8000, "channels": 1, "bit_depth": 16},
    "initial_format": {"codec": "pcm", "sample_rate": 8000, "channels": 1, "bit_depth": 24},
    "final_format": {"codec": "pcm", "sample_rate": 8000, "channels": 1, "bit_depth": 16},
    "stream_start_count": 2
  }
}
```

The client records the format it preferred, the format the stream started in, the
format it changed to, and how many stream formats it was started in.
