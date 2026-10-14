#pragma once

#include "args.h"
#include "fixture.h"
#include "server_session.h"
#include "trace_recorder.h"

#include <ArduinoJson.h>

#include <string>

namespace conformance {

/// @return whether this adapter drives the scenario
bool scenario_supported(const std::string& scenario_id);

/// Plays the scenario with the client that took the connection. Returns once the stream has
/// ended on the wire. Throws std::runtime_error naming why the scenario could not complete.
void drive_scenario(const Args& args, ServerSession& session, TraceRecorder& recorder,
                    const Fixture& fixture, const sendspin::server::ClientInfo& client,
                    Deadline deadline);

/// Adds the fields the scenario's verdict reads to summary, from what the trace showed of the
/// case connection
void add_scenario_evidence(JsonObject summary, const Args& args, const ConnectionRecord& record,
                           const Fixture& fixture);

}  // namespace conformance
