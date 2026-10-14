// Conformance server adapter for sendspin-server-cpp: the CLI the harness launches, and the
// summary it writes (adapters/README.md).

#include "args.h"
#include "fixture.h"
#include "json_file.h"
#include "scenarios.h"
#include "server_session.h"
#include "trace_recorder.h"

#include <ArduinoJson.h>

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <exception>
#include <iostream>
#include <map>
#include <mutex>
#include <string>
#include <thread>

using namespace conformance;

namespace {

using Clock = std::chrono::steady_clock;

// Waits end this long before --timeout-seconds, leaving time to stop the server and write the
// summary before the runner gives up on the process.
constexpr auto SCENARIO_MARGIN = std::chrono::seconds(3);
// The watchdog's own margin: it writes the summary and exits however the run is stuck.
constexpr auto WATCHDOG_MARGIN = std::chrono::milliseconds(500);

std::mutex summary_mutex;
bool summary_written = false;
std::mutex stage_mutex;
std::string stage = "starting";

void set_stage(const std::string& value) {
    std::lock_guard<std::mutex> lock(stage_mutex);
    stage = value;
}

/// Parses the harness's `--name value` pairs. Names this adapter does not use are logged and
/// ignored, as the harness may pass scenario arguments other adapters need.
Args parse_args(int argc, char** argv) {
    Args args;
    std::map<std::string, std::string*> strings = {
        {"summary", &args.summary},
        {"ready", &args.ready},
        {"registry", &args.registry},
        {"fixture", &args.fixture},
        {"scenario-id", &args.scenario_id},
        {"initiator-role", &args.initiator_role},
        {"preferred-codec", &args.preferred_codec},
        {"client-name", &args.client_name},
        {"client-id", &args.client_id},
        {"server-id", &args.server_id},
        {"server-name", &args.server_name},
    };
    for (int i = 1; i < argc; ++i) {
        const std::string flag = argv[i];
        if (flag.rfind("--", 0) != 0) {
            std::cerr << "ignoring argument " << flag << '\n';
            continue;
        }
        const std::string name = flag.substr(2);
        const bool has_value = i + 1 < argc && std::string(argv[i + 1]).rfind("--", 0) != 0;
        const std::string value = has_value ? argv[++i] : "true";
        if (const auto found = strings.find(name); found != strings.end()) {
            *found->second = value;
        } else if (name == "timeout-seconds") {
            args.timeout_seconds = std::stod(value);
        } else if (name == "clip-seconds") {
            args.clip_seconds = std::stod(value);
        } else if (name == "port") {
            args.port = std::stoi(value);
        } else {
            std::cerr << "ignoring --" << name << ' ' << value << '\n';
        }
    }
    return args;
}

/// Writes the summary unless one was written already, and prints it
void write_summary(const Args& args, const JsonDocument& summary) {
    std::lock_guard<std::mutex> lock(summary_mutex);
    if (summary_written) {
        return;
    }
    write_json_file(args.summary, summary);
    summary_written = true;
    serializeJsonPretty(summary, std::cout);
    std::cout << std::endl;
}

JsonDocument base_summary(const Args& args, const std::string& status) {
    JsonDocument summary;
    summary["status"] = status;
    summary["implementation"] = "sendspin-server-cpp";
    summary["role"] = "server";
    summary["server_name"] = args.server_name;
    summary["scenario_id"] = args.scenario_id;
    summary["initiator_role"] = args.initiator_role;
    summary["preferred_codec"] = args.preferred_codec;
    return summary;
}

/// The evidence every case is judged on, from the case connection as the trace showed it
void add_connection_evidence(JsonDocument& summary, const std::optional<ConnectionRecord>& record) {
    if (!record.has_value()) {
        summary["peer_hello"] = nullptr;
        return;
    }
    summary["peer_hello"] = parse_json(record->client_hello).as<JsonVariantConst>();
    summary["activation"] = parse_json(record->activation).as<JsonVariantConst>();
    JsonArray group_updates = summary["group_updates"].to<JsonArray>();
    for (const std::string& message : record->group_updates) {
        group_updates.add(parse_json(message).as<JsonVariantConst>());
    }
    JsonArray availability = summary["availability_trace"].to<JsonArray>();
    for (const AvailabilityEntry& entry : record->availability_trace) {
        JsonObject item = availability.add<JsonObject>();
        if (entry.stream_start) {
            item["type"] = "stream/start";
            item["phase"] = entry.sent ? "sent" : "sending";
            JsonArray roles = item["roles"].to<JsonArray>();
            for (const std::string& role : entry.roles) {
                roles.add(role);
            }
        } else {
            item["type"] = "client/state";
            if (entry.available.has_value()) {
                item["available"] = *entry.available;
            } else {
                item["available"] = nullptr;
            }
        }
    }
    JsonArray time_exchange = summary["time_exchange"].to<JsonArray>();
    for (const TimeEntry& entry : record->time_exchange) {
        if (entry.kind == TimeEntry::Kind::OTHER_SENT) {
            time_exchange.add<JsonObject>()["type"] = "other-sent";
        } else {
            time_exchange.add(parse_json(entry.json).as<JsonVariantConst>());
        }
    }
}

void watchdog(const Args& args, Clock::time_point deadline, const std::atomic<bool>& finished) {
    while (!finished.load()) {
        if (Clock::now() >= deadline) {
            JsonDocument summary = base_summary(args, "error");
            {
                std::lock_guard<std::mutex> lock(stage_mutex);
                summary["reason"] = "sendspin-server-cpp adapter did not finish within "
                                    "--timeout-seconds; it was " + stage;
            }
            try {
                write_summary(args, summary);
            } catch (const std::exception& err) {
                std::cerr << err.what() << '\n';
            }
            std::_Exit(1);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
}

int run(const Args& args, Clock::time_point started) {
    const auto timeout = std::chrono::duration_cast<Clock::duration>(
        std::chrono::duration<double>(args.timeout_seconds));
    const Clock::time_point deadline = started + timeout - SCENARIO_MARGIN;

    TraceRecorder recorder;
    std::string server_id;
    std::optional<sendspin::server::ClientInfo> client;
    std::string failure;
    try {
        if (!scenario_supported(args.scenario_id)) {
            throw std::runtime_error("sendspin-server-cpp server adapter does not support "
                                     "scenario " + args.scenario_id);
        }
        set_stage("decoding the fixture");
        const Fixture fixture = decode_fixture(args.fixture, args.clip_seconds);
        ServerSession session(args, recorder);
        set_stage("starting the server");
        session.start();
        server_id = session.server_id();
        set_stage(args.initiator_role == "client" ? "waiting for the client to connect"
                                                  : "waiting for the client's URL");
        session.open_connection(deadline);
        set_stage("waiting for the client to take the connection");
        client = session.wait_for_client(deadline);
        set_stage("streaming the fixture");
        try {
            drive_scenario(args, session, recorder, fixture, *client, deadline);
        } catch (const std::exception& err) {
            failure = err.what();
        }
        set_stage("stopping the server");
        session.stop();

        set_stage("writing the summary");
        JsonDocument summary = base_summary(args, failure.empty() ? "ok" : "error");
        if (!failure.empty()) {
            summary["reason"] = failure;
        }
        summary["server_id"] = server_id;
        summary["discovery_method"] = "registry";
        const std::optional<ConnectionRecord> record = recorder.snapshot();
        add_connection_evidence(summary, record);
        JsonObject peer = summary["client"].to<JsonObject>();
        peer["client_id"] = client->client_id;
        peer["name"] = client->name;
        peer["supported_roles"] = summary["peer_hello"]["payload"]["supported_roles"];
        JsonArray active_roles = peer["active_roles"].to<JsonArray>();
        for (const std::string& role :
             record.has_value() ? record->active_roles : std::vector<std::string>{}) {
            active_roles.add(role);
        }
        if (record.has_value()) {
            add_scenario_evidence(summary.as<JsonObject>(), args, *record, fixture);
        }
        write_summary(args, summary);
        return failure.empty() ? 0 : 1;
    } catch (const std::exception& err) {
        failure = err.what();
    }
    // Reached when the case ended before a client took the connection; the session has been
    // stopped, so the trace holds whatever got that far.
    JsonDocument summary = base_summary(args, "error");
    summary["reason"] = failure;
    if (!server_id.empty()) {
        summary["server_id"] = server_id;
    }
    add_connection_evidence(summary, recorder.snapshot());
    write_summary(args, summary);
    return 1;
}

}  // namespace

int main(int argc, char** argv) {
    const Clock::time_point started = Clock::now();
    const Args args = parse_args(argc, argv);
    if (args.summary.empty() || args.ready.empty() || args.registry.empty() ||
        args.fixture.empty()) {
        std::cerr << "usage: conformance-sendspin-server-cpp-server --summary <path> --ready "
                     "<path> --registry <path> --fixture <path> --scenario-id <id> [...]\n";
        return 2;
    }
    const auto timeout = std::chrono::duration_cast<Clock::duration>(
        std::chrono::duration<double>(args.timeout_seconds));
    std::atomic<bool> finished{false};
    std::thread guard(watchdog, std::cref(args), started + timeout - WATCHDOG_MARGIN,
                      std::cref(finished));
    const int code = run(args, started);
    finished.store(true);
    guard.join();
    return code;
}
