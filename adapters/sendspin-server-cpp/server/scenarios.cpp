#include "scenarios.h"

#include "json_file.h"

#include <stb_image_write.h>

#include <algorithm>
#include <chrono>
#include <map>
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
constexpr const char* SERVER_INITIATED_FLAC = "server-initiated-flac";
constexpr const char* SERVER_INITIATED_OPUS = "server-initiated-opus";
constexpr const char* CLIENT_INITIATED_STATE_FORMAT_PCM = "client-initiated-state-format-pcm";
constexpr const char* CLIENT_INITIATED_STATE_FORMAT_FLAC = "client-initiated-state-format-flac";
constexpr const char* SERVER_INITIATED_AUDIO_CHUNK_FRAMING =
    "server-initiated-audio-chunk-framing";
constexpr const char* SERVER_INITIATED_PROTOCOL_BASELINE =
    "server-initiated-protocol-baseline-v1";

constexpr const char* CLIENT_INITIATED_METADATA = "client-initiated-metadata";
constexpr const char* SERVER_INITIATED_METADATA = "server-initiated-metadata";
constexpr const char* CLIENT_INITIATED_ARTWORK = "client-initiated-artwork";
constexpr const char* SERVER_INITIATED_ARTWORK = "server-initiated-artwork";
constexpr const char* CLIENT_INITIATED_CONTROLLER = "client-initiated-controller";
constexpr const char* SERVER_INITIATED_CONTROLLER = "server-initiated-controller";

// The scenarios that stream the fixture to a player; each also judges the stream it got.
const std::set<std::string> PLAYER_STREAM_SCENARIOS = {
    CLIENT_INITIATED_PCM,
    SERVER_INITIATED_PCM,
    SERVER_INITIATED_PCM_24BIT,
    SERVER_INITIATED_FLAC,
    SERVER_INITIATED_OPUS,
    CLIENT_INITIATED_STATE_FORMAT_PCM,
    CLIENT_INITIATED_STATE_FORMAT_FLAC,
    SERVER_INITIATED_AUDIO_CHUNK_FRAMING,
    SERVER_INITIATED_PROTOCOL_BASELINE,
};

// The scenarios in which the client changes its preferred format mid-stream through client/state
const std::set<std::string> FORMAT_PREFERENCE_SCENARIOS = {
    CLIENT_INITIATED_STATE_FORMAT_PCM,
    CLIENT_INITIATED_STATE_FORMAT_FLAC,
};

// The scenarios judged on the encoded bytes the server sent, which the library encoded itself
const std::set<std::string> ENCODED_AUDIO_SCENARIOS = {
    SERVER_INITIATED_FLAC,
    SERVER_INITIATED_OPUS,
};

// The role each of the other scenarios exercises, with no audio
const std::map<std::string, std::string> STATE_SCENARIO_ROLES = {
    {CLIENT_INITIATED_METADATA, "metadata@v1"},  {SERVER_INITIATED_METADATA, "metadata@v1"},
    {CLIENT_INITIATED_ARTWORK, "artwork@v1"},    {SERVER_INITIATED_ARTWORK, "artwork@v1"},
    {CLIENT_INITIATED_CONTROLLER, "controller@v1"}, {SERVER_INITIATED_CONTROLLER, "controller@v1"},
};

constexpr auto PACED_BLOCK = std::chrono::milliseconds(100);
// How long the client is given to take in the last message before the server stops, which ends
// the connection
constexpr auto CLIENT_SETTLE = std::chrono::milliseconds(500);
constexpr int ARTWORK_JPEG_QUALITY = 85;
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

/// The group the client joined: the server's default group, as every case runs one client
GroupId case_group(SendspinServer& server) {
    const std::optional<GroupId> group = server.default_group();
    if (!group.has_value()) {
        throw std::runtime_error("the server has no default group");
    }
    return *group;
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
    InputOptions options;
    options.format = {fixture.sample_rate, fixture.channels, SampleFormat::S16};
    options.kind = InputKind::BUFFERED;
    OpenedInput opened = server.open_input(case_group(server), options);
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

/// Sets the --metadata-* track as the group's current metadata and waits until a server/state
/// carrying it is on the wire
void send_metadata(const Args& args, ServerSession& session, TraceRecorder& recorder,
                   Deadline deadline) {
    SendspinServer& server = session.server();
    Metadata metadata;
    metadata.title = args.metadata_title;
    metadata.artist = args.metadata_artist;
    metadata.album_artist = args.metadata_album_artist;
    metadata.album = args.metadata_album;
    metadata.artwork_url = args.metadata_artwork_url;
    metadata.year = static_cast<uint32_t>(args.metadata_year);
    metadata.track = static_cast<uint32_t>(args.metadata_track);
    metadata.progress = MetadataProgress{static_cast<uint32_t>(args.metadata_track_progress),
                                         static_cast<uint32_t>(args.metadata_track_duration),
                                         static_cast<uint32_t>(args.metadata_playback_speed)};
    if (!server.set_metadata(case_group(server), metadata, server.now_us())) {
        throw std::runtime_error("set_metadata() refused the --metadata-* track");
    }
    if (!recorder.wait_until(deadline, [&](const ConnectionRecord& record) {
            return parse_json(record.metadata)["title"] == args.metadata_title;
        })) {
        throw std::runtime_error("no server/state carrying the metadata set was written");
    }
}

/// A --artwork-width by --artwork-height JPEG of three horizontal bands
std::vector<uint8_t> encode_artwork(const Args& args) {
    if (args.artwork_format != "jpeg") {
        throw std::runtime_error("the adapter encodes --artwork-format jpeg only, not " +
                                 args.artwork_format);
    }
    if (args.artwork_width <= 0 || args.artwork_height <= 0) {
        throw std::runtime_error("--artwork-width and --artwork-height must be positive");
    }
    static const uint8_t BANDS[][3] = {{0xc9, 0x78, 0x3b}, {0x93, 0x52, 0x28}, {0x4b, 0x2f, 0x1b}};
    const size_t width = static_cast<size_t>(args.artwork_width);
    const size_t height = static_cast<size_t>(args.artwork_height);
    std::vector<uint8_t> rgb;
    rgb.reserve(width * height * 3);
    for (size_t y = 0; y < height; ++y) {
        const uint8_t* band = BANDS[y * std::size(BANDS) / height];
        for (size_t x = 0; x < width; ++x) {
            rgb.insert(rgb.end(), band, band + 3);
        }
    }
    std::vector<uint8_t> jpeg;
    const auto append = [](void* context, void* data, int size) {
        const auto* bytes = static_cast<const uint8_t*>(data);
        static_cast<std::vector<uint8_t>*>(context)->insert(
            static_cast<std::vector<uint8_t>*>(context)->end(), bytes, bytes + size);
    };
    if (stbi_write_jpg_to_func(append, &jpeg, args.artwork_width, args.artwork_height, 3,
                               rgb.data(), ARTWORK_JPEG_QUALITY) == 0) {
        throw std::runtime_error("stb_image_write could not encode the artwork");
    }
    return jpeg;
}

/// Sets the artwork as the group's album image and waits until a transfer of it is on the wire
void send_artwork(const Args& args, ServerSession& session, TraceRecorder& recorder,
                  Deadline deadline) {
    SendspinServer& server = session.server();
    const std::vector<uint8_t> image = encode_artwork(args);
    if (!server.set_artwork(case_group(server), ArtworkSource::ALBUM, image, server.now_us())) {
        throw std::runtime_error("set_artwork() refused the artwork");
    }
    if (!recorder.wait_until(deadline, [](const ConnectionRecord& record) {
            return std::any_of(record.artwork_images.begin(), record.artwork_images.end(),
                               [](const SentArtwork& sent) { return !sent.bytes.empty(); });
        })) {
        const std::optional<ConnectionRecord> record = recorder.snapshot();
        const bool cleared = record.has_value() && !record->artwork_images.empty();
        throw std::runtime_error(std::string("no artwork transfer of the image was written in "
                                             "full") +
                                 (cleared ? " (only an empty image, clearing the channel, was)"
                                          : "") +
                                 "; listener: " + joined(session.listener_reports()));
    }
}

/// @return the command the host carries out for a `client/command` name; nullopt for one the
/// library carries out itself, or none
std::optional<TransportCommand> transport_command(const std::string& name) {
    static const std::map<std::string, TransportCommand> COMMANDS = {
        {"play", TransportCommand::PLAY},
        {"pause", TransportCommand::PAUSE},
        {"stop", TransportCommand::STOP},
        {"next", TransportCommand::NEXT},
        {"previous", TransportCommand::PREVIOUS},
        {"repeat_off", TransportCommand::REPEAT_OFF},
        {"repeat_one", TransportCommand::REPEAT_ONE},
        {"repeat_all", TransportCommand::REPEAT_ALL},
        {"shuffle", TransportCommand::SHUFFLE},
        {"unshuffle", TransportCommand::UNSHUFFLE},
    };
    const auto found = COMMANDS.find(name);
    return found == COMMANDS.end() ? std::nullopt : std::optional(found->second);
}

/// @return the repeat mode a `server/state` controller `repeat` names; throws for another
RepeatMode repeat_mode(const std::string& name) {
    if (name == "off") {
        return RepeatMode::OFF;
    }
    if (name == "one") {
        return RepeatMode::ONE;
    }
    if (name == "all") {
        return RepeatMode::ALL;
    }
    throw std::runtime_error("unknown --controller-repeat " + name);
}

/// Offers --controller-command with the --controller-repeat and --controller-shuffle state and
/// waits for the client's client/command. A command the library carries out itself (`volume`,
/// `mute`, `switch`) is offered by the library, not through the caps.
void exchange_controller_command(const Args& args, ServerSession& session,
                                 TraceRecorder& recorder, Deadline deadline) {
    SendspinServer& server = session.server();
    ControllerCaps caps;
    static const std::set<std::string> LIBRARY_COMMANDS = {"volume", "mute", "switch"};
    const std::optional<TransportCommand> command = transport_command(args.controller_command);
    if (command.has_value()) {
        caps.commands.push_back(*command);
    } else if (LIBRARY_COMMANDS.count(args.controller_command) == 0) {
        throw std::runtime_error("the adapter cannot offer --controller-command " +
                                 args.controller_command);
    }
    caps.repeat = repeat_mode(args.controller_repeat);
    static const std::set<std::string> TRUE_VALUES = {"1", "true", "yes", "on"};
    caps.shuffle = TRUE_VALUES.count(args.controller_shuffle) != 0;
    if (!server.set_controller_caps(case_group(server), caps)) {
        throw std::runtime_error("set_controller_caps() refused the caps");
    }
    if (!recorder.wait_until(deadline, [](const ConnectionRecord& record) {
            return record.client_command.has_value();
        })) {
        throw std::runtime_error("the client sent no client/command with a controller object");
    }
    if (!recorder.wait_until(deadline, [&](const ConnectionRecord& record) {
            const JsonDocument state = parse_json(record.controller_state);
            for (JsonVariantConst offered : state["supported_commands"].as<JsonArrayConst>()) {
                if (offered == args.controller_command) {
                    return true;
                }
            }
            return false;
        })) {
        throw std::runtime_error("no server/state offering " + args.controller_command +
                                 " in its controller object was written");
    }
}

/// The metadata scenarios' fields: the last metadata state written and the first one's bound
void add_metadata_evidence(JsonObject summary, const ConnectionRecord& record) {
    const JsonDocument sent = parse_json(record.metadata);
    JsonObject metadata = summary["metadata"].to<JsonObject>();
    // The snapshot shape conformance.runner compares, with a field the state omitted as null
    JsonObject expected = metadata["expected"].to<JsonObject>();
    for (const char* field :
         {"title", "artist", "album_artist", "album", "artwork_url", "year", "track"}) {
        expected[field] = sent[field];
    }
    JsonVariantConst progress = sent["progress"];
    if (progress.is<JsonObjectConst>()) {
        for (const char* field : {"track_progress", "track_duration", "playback_speed"}) {
            expected["progress"][field] = progress[field];
        }
    } else {
        expected["progress"] = nullptr;
    }
    if (record.first_metadata_state.has_value()) {
        JsonObject first = metadata["first_state_sent"].to<JsonObject>();
        first["timestamp_us"] = record.first_metadata_state->timestamp_us;
        first["bound_us"] = record.first_metadata_state->bound_us;
    }
}

/// The artwork scenarios' fields: the last image sent in full, with its channel's configuration
void add_artwork_evidence(JsonObject summary, const ConnectionRecord& record) {
    JsonObject artwork = summary["artwork"].to<JsonObject>();
    const auto sent = std::find_if(record.artwork_images.rbegin(), record.artwork_images.rend(),
                                   [](const SentArtwork& image) { return !image.bytes.empty(); });
    if (sent == record.artwork_images.rend()) {
        artwork["channel"] = nullptr;
        artwork["encoded_sha256"] = nullptr;
        artwork["byte_count"] = 0;
        return;
    }
    artwork["channel"] = sent->channel;
    // The channel's configuration, which the library encoded the image to
    const JsonDocument stream = parse_json(record.stream_artwork);
    JsonVariantConst channel = stream["channels"][sent->channel];
    for (const char* field : {"source", "format", "width", "height"}) {
        artwork[field] = channel[field];
    }
    artwork["encoded_sha256"] = sha256_hex(sent->bytes.data(), sent->bytes.size());
    artwork["byte_count"] = sent->bytes.size();
}

/// The controller scenarios' fields: the command received and the last controller state written
void add_controller_evidence(JsonObject summary, const Args& args, const ConnectionRecord& record) {
    JsonObject controller = summary["controller"].to<JsonObject>();
    controller["expected_command"]["command"] = args.controller_command;
    controller["received_command"] = parse_json(record.client_command).as<JsonVariantConst>();
    const JsonDocument state = parse_json(record.controller_state);
    std::vector<std::string> commands;
    for (JsonVariantConst command : state["supported_commands"].as<JsonArrayConst>()) {
        commands.emplace_back(command | "");
    }
    std::sort(commands.begin(), commands.end());
    JsonArray supported = controller["supported_commands"].to<JsonArray>();
    for (const std::string& command : commands) {
        supported.add(command);
    }
    for (const char* field : {"volume", "muted", "repeat", "shuffle"}) {
        controller[field] = state[field];
    }
}

}  // namespace

bool scenario_supported(const std::string& scenario_id) {
    return PLAYER_STREAM_SCENARIOS.count(scenario_id) != 0 ||
           STATE_SCENARIO_ROLES.count(scenario_id) != 0;
}

void drive_scenario(const Args& args, ServerSession& session, TraceRecorder& recorder,
                    const Fixture& fixture, const ClientInfo& client, Deadline deadline) {
    // The client took the connection after the activation it answered, so the trace already
    // holds the roles that activation left active.
    const std::optional<ConnectionRecord> record = recorder.snapshot();
    const std::vector<std::string> active =
        record.has_value() ? record->active_roles : std::vector<std::string>{};
    const auto state_role = STATE_SCENARIO_ROLES.find(args.scenario_id);
    const std::string role =
        state_role == STATE_SCENARIO_ROLES.end() ? "player@v1" : state_role->second;
    if (std::find(active.begin(), active.end(), role) == active.end()) {
        throw std::runtime_error("the server did not activate " + role + " for the client "
                                 "(client/hello supported_roles it implements: " +
                                 joined(client.roles) + "; active_roles: " + joined(active) +
                                 "; listener: " + joined(session.listener_reports()) + ")");
    }
    if (state_role != STATE_SCENARIO_ROLES.end()) {
        if (role == "metadata@v1") {
            send_metadata(args, session, recorder, deadline);
        } else if (role == "artwork@v1") {
            send_artwork(args, session, recorder, deadline);
        } else {
            exchange_controller_command(args, session, recorder, deadline);
        }
        // The server's stop ends the connection, so the client gets time to take in what the
        // server sent last, and a controller command time to change the state both sides report.
        std::this_thread::sleep_until(std::min<Clock::time_point>(Clock::now() + CLIENT_SETTLE,
                                                                  deadline));
        return;
    }
    // The library holds stream/start until the client is available, but its timeline starts
    // when the input opens, so audio written to a client that becomes available later is lost.
    if (!recorder.wait_until(deadline, [](const ConnectionRecord& record) {
            return record.latest_available == true;
        })) {
        throw std::runtime_error("the client never reported available: true in client/state");
    }
    stream_fixture(session, recorder, fixture,
                   FORMAT_PREFERENCE_SCENARIOS.count(args.scenario_id) != 0, deadline);
}

void add_scenario_evidence(JsonObject summary, const Args& args, const ConnectionRecord& record,
                           const Fixture& fixture) {
    const auto state_role = STATE_SCENARIO_ROLES.find(args.scenario_id);
    if (state_role != STATE_SCENARIO_ROLES.end()) {
        if (state_role->second == "metadata@v1") {
            add_metadata_evidence(summary, record);
        } else if (state_role->second == "artwork@v1") {
            add_artwork_evidence(summary, record);
        } else {
            add_controller_evidence(summary, args, record);
        }
        return;
    }
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
    if (ENCODED_AUDIO_SCENARIOS.count(args.scenario_id) != 0) {
        // The audio chunk payloads as they went on the wire; the stream/start codec_header is
        // in summary["stream"], where the matrix reads it
        audio["sent_audio_chunk_count"] = record.sent_audio_chunk_count;
        audio["sent_encoded_byte_count"] = record.sent_audio.size();
        if (record.sent_audio_chunk_count != 0) {
            audio["sent_encoded_sha256"] =
                sha256_hex(record.sent_audio.data(), record.sent_audio.size());
        } else {
            audio["sent_encoded_sha256"] = nullptr;
        }
    }
    if (FORMAT_PREFERENCE_SCENARIOS.count(args.scenario_id) != 0) {
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
