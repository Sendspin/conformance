#pragma once

#include "sendspin/config.h"

#include <ArduinoJson.h>

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace conformance {

/// One message from sendspin-cpp's plaintext message trace (SendspinClientConfig::message_trace)
struct TraceEvent {
    uint64_t connection_id{0};
    bool inbound{false};
    /// False for a cleartext text frame, true for a message under the Noise transport
    bool binary{false};
    /// The leading type byte of a transport message; 0 for a cleartext one
    uint8_t message_id{0};
    /// The JSON `type`, or empty for a role binary message
    std::string type;
    /// The plaintext: the JSON text without its type byte, or a role binary message whole
    std::vector<uint8_t> plaintext;
    /// The encoded audio an inbound player audio chunk carried, as the library counted it
    size_t audio_bytes{0};
};

/// Keeps every traced message. The trace runs on the library's protocol task; the adapter reads
/// a snapshot once the session ends.
class TraceRecorder {
public:
    sendspin::SendspinMessageTraceFn callback();
    std::vector<TraceEvent> snapshot() const;

private:
    mutable std::mutex mu_;
    std::vector<TraceEvent> events_;
};

/// The messages of the latest connection that sent a client/hello, as the sendspin-server-cpp
/// adapter picks its own, or of the first connection traced when none did
std::vector<TraceEvent> session_events(const std::vector<TraceEvent>& events);

/// The first message of `type` in `direction`, parsed as {"type", "payload"}
std::optional<JsonDocument> first_json(const std::vector<TraceEvent>& events, bool inbound,
                                       const std::string& type);

/// Sets summary["stream"] from the first inbound stream/start player object, or null
void add_stream(JsonObject summary, const std::vector<TraceEvent>& events);

/// What the client received of the player stream, read from the trace
struct ReceivedAudio {
    size_t chunk_count{0};
    size_t encoded_byte_count{0};
    std::string encoded_sha256;
    /// Unset unless every chunk was PCM this adapter converts, which needs no decoder to hash
    std::optional<std::string> pcm_sha256;
    std::optional<size_t> sample_count;
    std::vector<int64_t> chunk_timestamps_us;
};

ReceivedAudio received_audio(const std::vector<TraceEvent>& events);

/// adapters/README.md "Audio chunk framing contract": each inbound role binary frame
void add_received_chunk_frames(JsonObject audio, const std::vector<TraceEvent>& events);

/// adapters/README.md "Protocol evidence contract", judged from the client's side
void add_protocol_evidence(JsonObject summary, const std::vector<TraceEvent>& events,
                           const ReceivedAudio& audio);

}  // namespace conformance
