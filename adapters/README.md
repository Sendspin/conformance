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
