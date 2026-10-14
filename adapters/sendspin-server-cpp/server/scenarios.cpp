#include "scenarios.h"

#include "json_file.h"

#include <algorithm>
#include <chrono>
#include <set>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

namespace conformance {

namespace {

using namespace sendspin::server;
using Clock = std::chrono::steady_clock;

// The revision conformance.protocol.SPEC_REVISION names, which protocol evidence must cite
constexpr const char* PROTOCOL_SPEC_REVISION = "8c9577ea8719ad082d051ec13cc73ef15ed68948";

constexpr const char* CLIENT_INITIATED_PCM = "client-initiated-pcm";
constexpr const char* SERVER_INITIATED_PCM = "server-initiated-pcm";
constexpr const char* SERVER_INITIATED_PCM_24BIT = "server-initiated-pcm-24bit";
constexpr const char* CLIENT_INITIATED_STATE_FORMAT_PCM = "client-initiated-state-format-pcm";
constexpr const char* SERVER_INITIATED_AUDIO_CHUNK_FRAMING =
    "server-initiated-audio-chunk-framing";
constexpr const char* SERVER_INITIATED_PROTOCOL_BASELINE =
    "server-initiated-protocol-baseline-v1";

// The scenarios that stream the fixture to a player; each also judges the stream it got.
const std::set<std::string> PLAYER_STREAM_SCENARIOS = {
    CLIENT_INITIATED_PCM,
    SERVER_INITIATED_PCM,
    SERVER_INITIATED_PCM_24BIT,
    CLIENT_INITIATED_STATE_FORMAT_PCM,
    SERVER_INITIATED_AUDIO_CHUNK_FRAMING,
    SERVER_INITIATED_PROTOCOL_BASELINE,
};

constexpr auto PACED_BLOCK = std::chrono::milliseconds(100);
constexpr auto WRITABLE_WAIT = std::chrono::milliseconds(500);
constexpr uint8_t FIRST_ROLE_MESSAGE_ID = 4;

// The role versions whose activation requires their support object in client/hello at the
// pinned revision (roles/player, roles/source and roles/visualizer define one)
const std::set<std::string> ROLES_WITH_SUPPORT_OBJECTS = {"player", "source", "visualizer"};

const char* input_state_name(InputState state) {
    switch (state) {
        case InputState::OPEN:
            return "open";
        case InputState::FULL:
            return "full";
        case InputState::DRAINING:
            return "draining";
        case InputState::STOP_LATCHED:
            return "stopped by the server";
        case InputState::CLOSED:
            return "closed";
    }
    return "unknown";
}

std::string joined(const std::vector<std::string>& items) {
    std::string out;
    for (const std::string& item : items) {
        out += (out.empty() ? "" : ", ") + item;
    }
    return out.empty() ? "none" : out;
}

/// Writes frames frames of s16 PCM, waiting while the input is full
void write_frames(GroupInput& input, const int16_t* pcm, size_t frames, uint8_t channels,
                  Deadline deadline) {
    size_t done = 0;
    while (done < frames) {
        const WriteResult result = input.write(pcm + done * channels, frames - done);
        done += result.frames;
        if (done == frames) {
            return;
        }
        if (result.state != InputState::OPEN && result.state != InputState::FULL) {
            throw std::runtime_error(std::string("the group input stopped taking audio (") +
                                     input_state_name(result.state) + ") after " +
                                     std::to_string(done) + " of " + std::to_string(frames) +
                                     " frames");
        }
        const auto now = Clock::now();
        if (now >= deadline) {
            throw std::runtime_error("the group input was still full at the deadline");
        }
        const auto wait = std::min<Clock::duration>(WRITABLE_WAIT, deadline - now);
        input.wait_writable(std::chrono::duration_cast<std::chrono::microseconds>(wait).count());
    }
}

/// Streams the whole fixture into the default group and waits until the stream has ended on
/// the wire. paced writes it in real time, so a change of the player's format mid-stream has
/// audio left to apply to; otherwise it is written as fast as the input takes it.
void stream_fixture(ServerSession& session, TraceRecorder& recorder, const Fixture& fixture,
                    bool paced, Deadline deadline) {
    SendspinServer& server = session.server();
    const std::optional<GroupId> group = server.default_group();
    if (!group.has_value()) {
        throw std::runtime_error("the server has no default group");
    }
    InputOptions options;
    options.format = {fixture.sample_rate, fixture.channels, SampleFormat::S16};
    options.kind = InputKind::BUFFERED;
    OpenedInput opened = server.open_input(*group, options);
    if (opened.result != OpenInputResult::OK) {
        throw std::runtime_error("open_input() failed: OpenInputResult " +
                                 std::to_string(static_cast<int>(opened.result)));
    }
    GroupInput& input = *opened.input;

    const size_t block_frames = paced ? std::max<size_t>(1, fixture.sample_rate / 10)
                                      : static_cast<size_t>(fixture.frame_count);
    auto next_block = Clock::now();
    for (size_t at = 0; at < fixture.frame_count;) {
        const size_t frames = std::min<size_t>(block_frames, fixture.frame_count - at);
        write_frames(input, fixture.samples.data() + at * fixture.channels, frames,
                     fixture.channels, deadline);
        at += frames;
        if (paced) {
            next_block += PACED_BLOCK;
            std::this_thread::sleep_until(std::min<Clock::time_point>(next_block, deadline));
        }
    }

    const DrainId drain = input.drain();
    const std::optional<DrainOutcome> outcome = session.wait_for_drain(drain, deadline);
    if (!outcome.has_value()) {
        throw std::runtime_error("the drain did not complete before the deadline");
    }
    if (*outcome != DrainOutcome::PLAYED) {
        throw std::runtime_error("the drain ended without playing out: DrainOutcome " +
                                 std::to_string(static_cast<int>(*outcome)));
    }
    if (!recorder.wait_until(deadline,
                             [](const ConnectionRecord& r) { return r.stream_end_written; })) {
        throw std::runtime_error("the drain completed but no stream/end write completed");
    }
}

JsonObject assertion(JsonObject assertions, const char* id) {
    JsonObject result = assertions[id].to<JsonObject>();
    result["status"] = "failed";
    result["detail"] = nullptr;
    result["events"].to<JsonArray>();
    return result;
}

void add_event(JsonObject result, const WireEvent& event, const char* name) {
    JsonObject entry = result["events"].as<JsonArray>().add<JsonObject>();
    entry["event"] = name;
    entry["direction"] = event.inbound ? "inbound" : "outbound";
    entry["message_type"] =
        event.type.empty() ? "binary:" + std::to_string(event.message_id) : event.type;
    entry["frame"] = event.binary ? "binary" : "text";
}

std::string event_label(const WireEvent& event) {
    return std::string(event.inbound ? "inbound " : "outbound ") +
           (event.type.empty() ? "binary message " + std::to_string(event.message_id)
                               : event.type) +
           " in a " + (event.binary ? "binary" : "text") + " frame";
}

std::optional<size_t> first_event(const ConnectionRecord& record, size_t from, bool inbound,
                                  const std::string& type) {
    for (size_t i = from; i < record.events.size(); ++i) {
        if (record.events[i].inbound == inbound && record.events[i].type == type) {
            return i;
        }
    }
    return std::nullopt;
}

/// CORE-001: messaging.md "Communication", in order, with the frame type each must use
void judge_handshake(JsonObject assertions, const ConnectionRecord& record) {
    struct Step {
        bool inbound;
        bool binary;
        const char* type;
    };
    static const Step STEPS[] = {
        {true, false, "client/init"},      {false, false, "server/init"},
        {false, false, "noise/handshake"}, {true, false, "noise/handshake"},
        {false, true, "server/hello"},     {true, true, "client/hello"},
        {false, true, "server/activate"},
    };
    JsonObject result = assertion(assertions, "CORE-001");
    const size_t step_count = std::size(STEPS);
    for (size_t i = 0; i < std::min(step_count, record.events.size()); ++i) {
        add_event(result, record.events[i], "handshake_message");
    }
    for (size_t i = 0; i < step_count; ++i) {
        const Step& step = STEPS[i];
        const std::string expected = std::string(step.inbound ? "inbound " : "outbound ") +
                                     step.type + " in a " + (step.binary ? "binary" : "text") +
                                     " frame";
        if (i >= record.events.size()) {
            result["detail"] = "the connection ended after " + std::to_string(i) +
                               " handshake messages; step " + std::to_string(i + 1) + " is " +
                               expected;
            return;
        }
        const WireEvent& event = record.events[i];
        if (event.inbound != step.inbound || event.binary != step.binary || event.type != step.type) {
            result["detail"] = "message " + std::to_string(i + 1) + " was " + event_label(event) +
                               " where step " + std::to_string(i + 1) + " is " + expected;
            return;
        }
    }
    result["status"] = "passed";
    result["detail"] = "the trace shows the seven handshake messages in order, the cleartext ones "
                       "in text frames and the encrypted ones in binary frames";
}

/// CORE-002: nothing but the handshake messages (or a client/goodbye) before server/activate
void judge_nothing_before_activation(JsonObject assertions, const ConnectionRecord& record) {
    static const std::set<std::string> HANDSHAKE = {"client/init", "server/init",
                                                    "noise/handshake", "server/hello",
                                                    "client/hello", "client/goodbye"};
    JsonObject result = assertion(assertions, "CORE-002");
    const std::optional<size_t> activation = first_event(record, 0, false, "server/activate");
    if (!activation.has_value()) {
        for (const WireEvent& event : record.events) {
            add_event(result, event, "message_without_activation");
        }
        result["detail"] = "the server sent no server/activate";
        return;
    }
    for (size_t i = 0; i <= *activation; ++i) {
        add_event(result, record.events[i], "message_before_activation");
    }
    for (size_t i = 0; i < *activation; ++i) {
        if (HANDSHAKE.count(record.events[i].type) == 0) {
            result["detail"] = event_label(record.events[i]) +
                               " was exchanged before the first server/activate";
            return;
        }
    }
    result["status"] = "passed";
    result["detail"] = "only handshake messages preceded the first server/activate";
}

/// CORE-003: active_roles lists only roles the client advertised, each with the support object
/// its role requires
void judge_activated_roles(JsonObject assertions, const ConnectionRecord& record) {
    JsonObject result = assertion(assertions, "CORE-003");
    const JsonDocument activation = parse_json(record.activation);
    const JsonDocument hello = parse_json(record.client_hello);
    JsonVariantConst active_roles = activation["payload"]["active_roles"];
    JsonVariantConst supported_roles = hello["payload"]["supported_roles"];
    JsonObject event = result["events"].as<JsonArray>().add<JsonObject>();
    event["event"] = "server_activate_checked_against_client_hello";
    event["active_roles"] = active_roles;
    event["supported_roles"] = supported_roles;
    JsonArray support_objects = event["support_objects"].to<JsonArray>();
    for (JsonPairConst field : hello["payload"].as<JsonObjectConst>()) {
        const std::string key = field.key().c_str();
        if (key.size() > 8 && key.compare(key.size() - 8, 8, "_support") == 0) {
            support_objects.add(key);
        }
    }
    if (!record.activation.has_value() || !record.client_hello.has_value()) {
        result["detail"] = record.activation.has_value() ? "no client/hello was observed"
                                                         : "the server sent no server/activate";
        return;
    }
    if (!active_roles.is<JsonArrayConst>()) {
        result["detail"] = "the first server/activate carried no active_roles list";
        return;
    }
    std::set<std::string> advertised;
    for (JsonVariantConst role : supported_roles.as<JsonArrayConst>()) {
        advertised.insert(role | "");
    }
    for (JsonVariantConst value : active_roles.as<JsonArrayConst>()) {
        const std::string role = value | "";
        if (advertised.count(role) == 0) {
            result["detail"] = "the server activated " + role +
                               ", which client/hello supported_roles does not list";
            return;
        }
        const std::string family = role.substr(0, role.find('@'));
        if (ROLES_WITH_SUPPORT_OBJECTS.count(family) != 0 &&
            !hello["payload"][role + "_support"].is<JsonObjectConst>()) {
            result["detail"] = "the server activated " + role + " though client/hello carried no " +
                               role + "_support object";
            return;
        }
    }
    result["status"] = "passed";
    result["detail"] = "every active role was advertised, with the support object its role "
                       "requires";
}

/// CORE-004: the client's initial client/state came before the server sent role binary data
void judge_initial_client_state(JsonObject assertions, const ConnectionRecord& record) {
    JsonObject result = assertion(assertions, "CORE-004");
    const std::optional<size_t> activation = first_event(record, 0, false, "server/activate");
    if (!activation.has_value()) {
        result["detail"] = "the server sent no server/activate";
        return;
    }
    add_event(result, record.events[*activation], "server_activate");
    const std::optional<size_t> state = first_event(record, *activation, true, "client/state");
    std::optional<size_t> binary;
    for (size_t i = *activation; i < record.events.size(); ++i) {
        const WireEvent& event = record.events[i];
        if (!event.inbound && event.message_id >= FIRST_ROLE_MESSAGE_ID) {
            binary = i;
            break;
        }
    }
    if (state.has_value()) {
        add_event(result, record.events[*state], "initial_client_state");
    }
    if (binary.has_value()) {
        add_event(result, record.events[*binary], "first_role_binary_data");
    }
    if (!state.has_value()) {
        result["detail"] = "no client/state followed the first server/activate";
    } else if (!binary.has_value()) {
        result["detail"] = "the server sent no role binary data, so the ordering was not "
                           "exercised";
    } else if (*binary < *state) {
        result["detail"] = "the server sent role binary data before the client's initial "
                           "client/state";
    } else {
        result["status"] = "passed";
        result["detail"] = "the initial client/state preceded the first role binary data";
    }
}

/// PLAYER-001: the stream/start format is one client/hello listed, followed by audio chunks
/// whose timestamps do not go backwards
void judge_player_stream(JsonObject assertions, const ConnectionRecord& record) {
    JsonObject result = assertion(assertions, "PLAYER-001");
    const JsonDocument hello = parse_json(record.client_hello);
    const JsonDocument stream = parse_json(record.stream_player);
    JsonVariantConst advertised = hello["payload"]["player@v1_support"]["supported_formats"];
    JsonObject started = result["events"].as<JsonArray>().add<JsonObject>();
    started["event"] = "stream_start_observed";
    started["negotiated_format"] = stream.as<JsonVariantConst>();
    started["advertised_formats"] = advertised;
    if (!record.stream_player.has_value()) {
        result["detail"] = "no stream/start with a player object was observed";
        return;
    }
    const auto matches = [&](JsonVariantConst format) {
        const bool opus = stream["codec"] == "opus";
        return format["codec"] == stream["codec"] &&
               format["sample_rate"] == stream["sample_rate"] &&
               format["channels"] == stream["channels"] &&
               (opus || format["bit_depth"] == stream["bit_depth"]);
    };
    bool listed = false;
    for (JsonVariantConst format : advertised.as<JsonArrayConst>()) {
        listed = listed || matches(format);
    }
    if (!listed) {
        result["detail"] = "the stream/start format is not one client/hello supported_formats "
                           "lists";
        return;
    }
    const std::vector<int64_t>& timestamps = record.chunk_timestamps_us;
    JsonObject chunks = result["events"].as<JsonArray>().add<JsonObject>();
    chunks["event"] = "audio_chunks_observed";
    chunks["chunk_count"] = timestamps.size();
    if (timestamps.empty()) {
        result["detail"] = "no audio chunks were observed after stream/start";
        return;
    }
    chunks["first_timestamp_us"] = timestamps.front();
    chunks["last_timestamp_us"] = timestamps.back();
    for (size_t i = 1; i < timestamps.size(); ++i) {
        if (timestamps[i] < timestamps[i - 1]) {
            result["detail"] = "audio chunk " + std::to_string(i + 1) +
                               " is timestamped before the chunk ahead of it";
            return;
        }
    }
    result["status"] = "passed";
    result["detail"] = "selected an advertised format and sent " +
                       std::to_string(timestamps.size()) + " audio chunks in timestamp order";
}

}  // namespace

bool scenario_supported(const std::string& scenario_id) {
    return PLAYER_STREAM_SCENARIOS.count(scenario_id) != 0;
}

void drive_scenario(const Args& args, ServerSession& session, TraceRecorder& recorder,
                    const Fixture& fixture, const ClientInfo& client, Deadline deadline) {
    // The client took the connection after the activation it answered, so the trace already
    // holds the roles that activation left active.
    const std::optional<ConnectionRecord> record = recorder.snapshot();
    const std::vector<std::string> active =
        record.has_value() ? record->active_roles : std::vector<std::string>{};
    if (std::find(active.begin(), active.end(), "player@v1") == active.end()) {
        throw std::runtime_error("the server did not activate player@v1 for the client (client/hello "
                                 "supported_roles it implements: " + joined(client.roles) +
                                 "; active_roles: " + joined(active) + "; listener: " +
                                 joined(session.listener_reports()) + ")");
    }
    // The library holds stream/start until the client is available, but its timeline starts
    // when the input opens, so audio written to a client that becomes available later is lost.
    if (!recorder.wait_until(deadline, [](const ConnectionRecord& record) {
            return record.latest_available == true;
        })) {
        throw std::runtime_error("the client never reported available: true in client/state");
    }
    stream_fixture(session, recorder, fixture,
                   args.scenario_id == CLIENT_INITIATED_STATE_FORMAT_PCM, deadline);
}

void add_scenario_evidence(JsonObject summary, const Args& args, const ConnectionRecord& record,
                           const Fixture& fixture) {
    summary["stream"] = parse_json(record.stream_player).as<JsonVariantConst>();

    JsonObject audio = summary["audio"].to<JsonObject>();
    audio["fixture"] = fixture.path;
    audio["source_flac_sha256"] = fixture.source_flac_sha256;
    audio["source_pcm_sha256"] = fixture.source_pcm_sha256;
    audio["clip_seconds"] = args.clip_seconds;
    audio["sample_rate"] = fixture.sample_rate;
    audio["channels"] = fixture.channels;
    audio["bit_depth"] = 16;
    audio["frame_count"] = fixture.frame_count;
    audio["duration_seconds"] =
        static_cast<double>(fixture.frame_count) / static_cast<double>(fixture.sample_rate);

    if (args.scenario_id == SERVER_INITIATED_AUDIO_CHUNK_FRAMING) {
        JsonArray frames = audio["sent_chunk_frames"].to<JsonArray>();
        for (const SentFrame& frame : record.sent_frames) {
            JsonObject entry = frames.add<JsonObject>();
            entry["byte_count"] = frame.byte_count;
            entry["leading_hex"] = frame.leading_hex;
            if (frame.payload_byte_count.has_value()) {
                entry["payload_byte_count"] = *frame.payload_byte_count;
            } else {
                entry["payload_byte_count"] = nullptr;
            }
        }
        // The library cuts the input into chunks itself
        audio["chunked_by"] = "implementation";
        audio["legacy_header_reason"] = nullptr;
    }
    if (args.scenario_id == CLIENT_INITIATED_STATE_FORMAT_PCM) {
        summary["format_preference"]["received"] =
            parse_json(record.preferred_format).as<JsonVariantConst>();
    }
    if (args.scenario_id == SERVER_INITIATED_PROTOCOL_BASELINE) {
        JsonObject protocol = summary["protocol"].to<JsonObject>();
        protocol["spec_revision"] = PROTOCOL_SPEC_REVISION;
        JsonObject assertions = protocol["assertions"].to<JsonObject>();
        judge_handshake(assertions, record);
        judge_nothing_before_activation(assertions, record);
        judge_activated_roles(assertions, record);
        judge_initial_client_state(assertions, record);
        judge_player_stream(assertions, record);
    }
}

}  // namespace conformance
