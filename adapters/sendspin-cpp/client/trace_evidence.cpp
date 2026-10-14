#include "trace_evidence.h"

#include "sha256.h"

#include <algorithm>
#include <cstring>
#include <iterator>
#include <set>
#include <utility>

namespace conformance {

namespace {

using sendspin::SendspinTracedMessage;
using sendspin::SendspinTraceDirection;

// The revision conformance.protocol.SPEC_REVISION names, which protocol evidence must cite
constexpr const char* PROTOCOL_SPEC_REVISION = "8c9577ea8719ad082d051ec13cc73ef15ed68948";

constexpr uint8_t JSON_MESSAGE_ID = 0;
constexpr uint8_t AUDIO_CHUNK_MESSAGE_ID = 4;
constexpr uint8_t FIRST_ROLE_MESSAGE_ID = 4;
// adapters/README.md "Audio chunk framing contract": the leading bytes each frame record carries
constexpr size_t FRAME_PREFIX_BYTES = 13;

// The role versions whose activation requires their support object in client/hello at the
// pinned revision (roles/player, roles/source and roles/visualizer define one)
const std::set<std::string> ROLES_WITH_SUPPORT_OBJECTS = {"player", "source", "visualizer"};

std::string json_type(const uint8_t* data, size_t len) {
    JsonDocument doc;
    if (deserializeJson(doc, reinterpret_cast<const char*>(data), len)) {
        return {};
    }
    return doc["type"] | "";
}

int64_t read_be_int64(const uint8_t* data) {
    uint64_t value = 0;
    for (size_t i = 0; i < 8; ++i) {
        value = (value << 8) | data[i];
    }
    return static_cast<int64_t>(value);
}

/// adapters/README.md "Audio chunk framing contract": the first 13 bytes, or the whole frame
std::string leading_hex(const std::vector<uint8_t>& frame) {
    return hex_lower(frame.data(), std::min(frame.size(), FRAME_PREFIX_BYTES));
}

/// Appends little-endian integer PCM to `hasher` as canonical float32, as conformance.pcm does
/// @return The samples hashed, or nullopt for a depth it does not convert or a partial sample,
///         which conformance.pcm rejects too
std::optional<size_t> hash_canonical_pcm(Sha256Hasher& hasher, const uint8_t* pcm, size_t len,
                                         int bit_depth) {
    const size_t width = static_cast<size_t>(bit_depth / 8);
    if ((width != 2 && width != 3 && width != 4) || bit_depth % 8 != 0 || len % width != 0) {
        return std::nullopt;
    }
    const double scale = static_cast<double>(uint64_t{1} << (width * 8 - 1));
    size_t samples = 0;
    for (size_t at = 0; at < len; at += width) {
        uint32_t bits = 0;
        for (size_t i = 0; i < width; ++i) {
            bits |= static_cast<uint32_t>(pcm[at + i]) << (8 * i);
        }
        int64_t value = bits;
        const int64_t sign_bit = int64_t{1} << (width * 8 - 1);
        if ((value & sign_bit) != 0) {
            value -= sign_bit << 1;
        }
        const auto sample = static_cast<float>(static_cast<double>(value) / scale);
        uint8_t bytes[sizeof(float)];
        std::memcpy(bytes, &sample, sizeof(float));
        hasher.update(bytes, sizeof(float));
        ++samples;
    }
    return samples;
}

JsonObject assertion(JsonObject assertions, const char* id) {
    JsonObject result = assertions[id].to<JsonObject>();
    result["status"] = "failed";
    result["detail"] = nullptr;
    result["events"].to<JsonArray>();
    return result;
}

std::string message_label(const TraceEvent& event) {
    return event.type.empty() ? "binary:" + std::to_string(event.message_id) : event.type;
}

void add_event(JsonObject result, const TraceEvent& event, const char* name) {
    JsonObject entry = result["events"].as<JsonArray>().add<JsonObject>();
    entry["event"] = name;
    entry["direction"] = event.inbound ? "inbound" : "outbound";
    entry["message_type"] = message_label(event);
    entry["frame"] = event.binary ? "binary" : "text";
}

std::string event_label(const TraceEvent& event) {
    return std::string(event.inbound ? "inbound " : "outbound ") +
           (event.type.empty() ? "binary message " + std::to_string(event.message_id)
                               : event.type) +
           " in a " + (event.binary ? "binary" : "text") + " frame";
}

std::optional<size_t> first_event(const std::vector<TraceEvent>& events, size_t from,
                                  bool inbound, const std::string& type) {
    for (size_t i = from; i < events.size(); ++i) {
        if (events[i].inbound == inbound && events[i].type == type) {
            return i;
        }
    }
    return std::nullopt;
}

/// CORE-001: messaging.md "Communication", in order, with the frame type each must use
void judge_handshake(JsonObject assertions, const std::vector<TraceEvent>& events) {
    struct Step {
        bool inbound;
        bool binary;
        const char* type;
    };
    static const Step STEPS[] = {
        {false, false, "client/init"},    {true, false, "server/init"},
        {true, false, "noise/handshake"}, {false, false, "noise/handshake"},
        {true, true, "server/hello"},     {false, true, "client/hello"},
        {true, true, "server/activate"},
    };
    JsonObject result = assertion(assertions, "CORE-001");
    const size_t step_count = std::size(STEPS);
    for (size_t i = 0; i < std::min(step_count, events.size()); ++i) {
        add_event(result, events[i], "handshake_message");
    }
    for (size_t i = 0; i < step_count; ++i) {
        const Step& step = STEPS[i];
        const std::string expected = std::string(step.inbound ? "inbound " : "outbound ") +
                                     step.type + " in a " + (step.binary ? "binary" : "text") +
                                     " frame";
        if (i >= events.size()) {
            result["detail"] = "the connection ended after " + std::to_string(i) +
                               " handshake messages; step " + std::to_string(i + 1) + " is " +
                               expected;
            return;
        }
        const TraceEvent& event = events[i];
        if (event.inbound != step.inbound || event.binary != step.binary ||
            event.type != step.type) {
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
void judge_nothing_before_activation(JsonObject assertions, const std::vector<TraceEvent>& events) {
    static const std::set<std::string> HANDSHAKE = {"client/init", "server/init",
                                                    "noise/handshake", "server/hello",
                                                    "client/hello", "client/goodbye"};
    JsonObject result = assertion(assertions, "CORE-002");
    const std::optional<size_t> activation = first_event(events, 0, true, "server/activate");
    if (!activation.has_value()) {
        for (const TraceEvent& event : events) {
            add_event(result, event, "message_without_activation");
        }
        result["detail"] = "the client received no server/activate";
        return;
    }
    for (size_t i = 0; i <= *activation; ++i) {
        add_event(result, events[i], "message_before_activation");
    }
    for (size_t i = 0; i < *activation; ++i) {
        if (HANDSHAKE.count(events[i].type) == 0) {
            result["detail"] =
                event_label(events[i]) + " was exchanged before the first server/activate";
            return;
        }
    }
    result["status"] = "passed";
    result["detail"] = "only handshake messages preceded the first server/activate";
}

/// CORE-003: active_roles lists only roles the client advertised, each with the support object
/// its role requires
void judge_activated_roles(JsonObject assertions, const std::vector<TraceEvent>& events) {
    JsonObject result = assertion(assertions, "CORE-003");
    const std::optional<JsonDocument> activation = first_json(events, true, "server/activate");
    const std::optional<JsonDocument> hello = first_json(events, false, "client/hello");
    if (!activation.has_value() || !hello.has_value()) {
        result["detail"] = activation.has_value() ? "the client sent no client/hello"
                                                  : "the client received no server/activate";
        return;
    }
    JsonVariantConst active_roles = (*activation)["payload"]["active_roles"];
    JsonVariantConst supported_roles = (*hello)["payload"]["supported_roles"];
    JsonObject event = result["events"].as<JsonArray>().add<JsonObject>();
    event["event"] = "server_activate_checked_against_client_hello";
    event["active_roles"] = active_roles;
    event["supported_roles"] = supported_roles;
    JsonArray support_objects = event["support_objects"].to<JsonArray>();
    for (JsonPairConst field : (*hello)["payload"].as<JsonObjectConst>()) {
        const std::string key = field.key().c_str();
        if (key.size() > 8 && key.compare(key.size() - 8, 8, "_support") == 0) {
            support_objects.add(key);
        }
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
            !(*hello)["payload"][role + "_support"].is<JsonObjectConst>()) {
            result["detail"] = "the server activated " + role + " though client/hello carried no " +
                               role + "_support object";
            return;
        }
    }
    result["status"] = "passed";
    result["detail"] = "every active role was advertised, with the support object its role "
                       "requires";
}

/// CORE-004: the client's initial client/state went out before it received role binary data
void judge_initial_client_state(JsonObject assertions, const std::vector<TraceEvent>& events) {
    JsonObject result = assertion(assertions, "CORE-004");
    const std::optional<size_t> activation = first_event(events, 0, true, "server/activate");
    if (!activation.has_value()) {
        result["detail"] = "the client received no server/activate";
        return;
    }
    add_event(result, events[*activation], "server_activate");
    const std::optional<size_t> state = first_event(events, *activation, false, "client/state");
    std::optional<size_t> binary;
    for (size_t i = *activation; i < events.size(); ++i) {
        if (events[i].inbound && events[i].message_id >= FIRST_ROLE_MESSAGE_ID) {
            binary = i;
            break;
        }
    }
    if (state.has_value()) {
        add_event(result, events[*state], "initial_client_state");
    }
    if (binary.has_value()) {
        add_event(result, events[*binary], "first_role_binary_data");
    }
    if (!state.has_value()) {
        result["detail"] = "the client sent no client/state after the first server/activate";
    } else if (!binary.has_value()) {
        result["detail"] = "the client received no role binary data, so the ordering was not "
                           "exercised";
    } else if (*binary < *state) {
        result["detail"] = "the client received role binary data before it sent its initial "
                           "client/state";
    } else {
        result["status"] = "passed";
        result["detail"] = "the initial client/state preceded the first role binary data";
    }
}

/// PLAYER-001: the stream/start format is one client/hello listed, followed by audio chunks
/// whose timestamps do not go backwards
void judge_player_stream(JsonObject assertions, const std::vector<TraceEvent>& events,
                         const ReceivedAudio& audio) {
    JsonObject result = assertion(assertions, "PLAYER-001");
    const std::optional<JsonDocument> hello = first_json(events, false, "client/hello");
    const std::optional<JsonDocument> start = first_json(events, true, "stream/start");
    JsonVariantConst advertised =
        hello.has_value() ? (*hello)["payload"]["player@v1_support"]["supported_formats"]
                          : JsonVariantConst();
    JsonVariantConst stream =
        start.has_value() ? (*start)["payload"]["player"] : JsonVariantConst();
    JsonObject started = result["events"].as<JsonArray>().add<JsonObject>();
    started["event"] = "stream_start_observed";
    started["negotiated_format"] = stream;
    started["advertised_formats"] = advertised;
    if (!stream.is<JsonObjectConst>()) {
        result["detail"] = "no stream/start with a player object was received";
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
    const std::vector<int64_t>& timestamps = audio.chunk_timestamps_us;
    JsonObject chunks = result["events"].as<JsonArray>().add<JsonObject>();
    chunks["event"] = "audio_chunks_observed";
    chunks["chunk_count"] = timestamps.size();
    if (timestamps.empty()) {
        result["detail"] = "no audio chunks were received after stream/start";
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
    result["detail"] = "the server selected an advertised format and sent " +
                       std::to_string(timestamps.size()) + " audio chunks in timestamp order";
}

}  // namespace

sendspin::SendspinMessageTraceFn TraceRecorder::callback() {
    return [this](const SendspinTracedMessage& message) {
        TraceEvent event;
        event.connection_id = message.connection_id;
        event.inbound = message.direction == SendspinTraceDirection::INBOUND;
        event.binary = message.binary;
        const uint8_t* data = message.plaintext;
        size_t len = message.plaintext_len;
        if (message.binary && len > 0) {
            event.message_id = data[0];
            if (event.message_id == JSON_MESSAGE_ID) {
                ++data;
                --len;
            }
        }
        if (!message.binary || event.message_id == JSON_MESSAGE_ID) {
            event.type = json_type(data, len);
        }
        event.plaintext.assign(data, data + len);
        event.audio_bytes = message.audio_bytes;
        std::lock_guard<std::mutex> lock(mu_);
        events_.push_back(std::move(event));
    };
}

std::vector<TraceEvent> TraceRecorder::snapshot() const {
    std::lock_guard<std::mutex> lock(mu_);
    return events_;
}

std::vector<TraceEvent> session_events(const std::vector<TraceEvent>& events) {
    if (events.empty()) {
        return {};
    }
    uint64_t connection_id = events.front().connection_id;
    for (const TraceEvent& event : events) {
        if (!event.inbound && event.type == "client/hello") {
            connection_id = event.connection_id;
        }
    }
    std::vector<TraceEvent> session;
    for (const TraceEvent& event : events) {
        if (event.connection_id == connection_id) {
            session.push_back(event);
        }
    }
    return session;
}

std::optional<JsonDocument> first_json(const std::vector<TraceEvent>& events, bool inbound,
                                       const std::string& type) {
    for (const TraceEvent& event : events) {
        if (event.inbound == inbound && event.type == type) {
            JsonDocument doc;
            deserializeJson(doc, reinterpret_cast<const char*>(event.plaintext.data()),
                            event.plaintext.size());
            return doc;
        }
    }
    return std::nullopt;
}

void add_stream(JsonObject summary, const std::vector<TraceEvent>& events) {
    const std::optional<JsonDocument> start = first_json(events, true, "stream/start");
    if (!start.has_value() || !(*start)["payload"]["player"].is<JsonObjectConst>()) {
        summary["stream"] = nullptr;
        return;
    }
    JsonVariantConst player = (*start)["payload"]["player"];
    JsonObject stream = summary["stream"].to<JsonObject>();
    for (const char* key : {"codec", "sample_rate", "channels", "bit_depth", "codec_header"}) {
        stream[key] = player[key];
    }
}

ReceivedAudio received_audio(const std::vector<TraceEvent>& events) {
    ReceivedAudio audio;
    Sha256Hasher encoded;
    Sha256Hasher pcm;
    std::string codec;
    int bit_depth = 0;
    bool pcm_hashable = true;
    size_t samples = 0;
    for (const TraceEvent& event : events) {
        if (event.inbound && event.type == "stream/start") {
            JsonDocument doc;
            deserializeJson(doc, reinterpret_cast<const char*>(event.plaintext.data()),
                            event.plaintext.size());
            JsonVariantConst player = doc["payload"]["player"];
            if (player.is<JsonObjectConst>()) {
                codec = player["codec"] | "";
                bit_depth = player["bit_depth"] | 0;
                pcm_hashable = pcm_hashable && codec == "pcm";
            }
            continue;
        }
        if (!event.inbound || event.message_id != AUDIO_CHUNK_MESSAGE_ID) {
            continue;
        }
        const size_t size = event.plaintext.size();
        if (size < 1 + 8 || event.audio_bytes > size) {
            continue;
        }
        ++audio.chunk_count;
        audio.chunk_timestamps_us.push_back(read_be_int64(event.plaintext.data() + 1));
        // The audio follows the header, by the library's own count of the audio it carried
        const uint8_t* payload = event.plaintext.data() + (size - event.audio_bytes);
        encoded.update(payload, event.audio_bytes);
        audio.encoded_byte_count += event.audio_bytes;
        if (codec == "pcm" && pcm_hashable) {
            const std::optional<size_t> hashed =
                hash_canonical_pcm(pcm, payload, event.audio_bytes, bit_depth);
            pcm_hashable = hashed.has_value();
            samples += hashed.value_or(0);
        }
    }
    audio.encoded_sha256 = encoded.hexdigest();
    if (pcm_hashable && !codec.empty()) {
        audio.pcm_sha256 = pcm.hexdigest();
        audio.sample_count = samples;
    }
    return audio;
}

void add_received_chunk_frames(JsonObject audio, const std::vector<TraceEvent>& events) {
    JsonArray frames = audio["received_chunk_frames"].to<JsonArray>();
    for (const TraceEvent& event : events) {
        if (!event.inbound || !event.binary || event.message_id == JSON_MESSAGE_ID) {
            continue;
        }
        JsonObject frame = frames.add<JsonObject>();
        frame["byte_count"] = event.plaintext.size();
        frame["leading_hex"] = leading_hex(event.plaintext);
        if (event.message_id == AUDIO_CHUNK_MESSAGE_ID) {
            frame["payload_byte_count"] = event.audio_bytes;
        } else {
            frame["payload_byte_count"] = nullptr;
        }
    }
}

void add_protocol_evidence(JsonObject summary, const std::vector<TraceEvent>& events,
                           const ReceivedAudio& audio) {
    JsonObject protocol = summary["protocol"].to<JsonObject>();
    protocol["spec_revision"] = PROTOCOL_SPEC_REVISION;
    JsonObject assertions = protocol["assertions"].to<JsonObject>();
    judge_handshake(assertions, events);
    judge_nothing_before_activation(assertions, events);
    judge_activated_roles(assertions, events);
    judge_initial_client_state(assertions, events);
    judge_player_stream(assertions, events, audio);
}

}  // namespace conformance
