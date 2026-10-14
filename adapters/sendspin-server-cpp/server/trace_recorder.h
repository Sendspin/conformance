#pragma once

#include <sendspin/server/config.h>

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace conformance {

using Deadline = std::chrono::steady_clock::time_point;

/// One message on a connection, in the order the trace reported it
struct WireEvent {
    bool inbound{false};
    /// False for a cleartext WebSocket text frame
    bool binary{false};
    /// A binary message's ID (its first plaintext byte); 0 for a JSON body
    uint8_t message_id{0};
    /// The JSON `type`; empty for a message that is not JSON
    std::string type;
};

/// One `client/state` received or one `stream/start` sent (adapters/README.md "Availability
/// trace")
struct AvailabilityEntry {
    bool stream_start{false};
    /// client/state: the `available` it carried, nullopt when it carried no boolean
    std::optional<bool> available;
    /// stream/start: false for the `sending` entry, true for the `sent` one
    bool sent{false};
    /// stream/start: the role objects it carried
    std::vector<std::string> roles;
};

/// One entry of the time exchange (adapters/README.md "Time exchange")
struct TimeEntry {
    enum class Kind { CLIENT_TIME, SERVER_TIME, OTHER_SENT };
    Kind kind{Kind::CLIENT_TIME};
    /// The message as it crossed the transport; empty for OTHER_SENT
    std::string json;
};

/// One binary frame the server sent that is not a JSON body (adapters/README.md "Audio chunk
/// framing contract")
struct SentFrame {
    size_t byte_count{0};
    std::string leading_hex;
    /// The audio an audio chunk carried, as the library counted it; nullopt for any other frame
    std::optional<size_t> payload_byte_count;
};

/// The first `server/state` sent whose metadata object carried a timestamp (adapters/README.md
/// "Metadata scenario summary fields")
struct FirstMetadataState {
    int64_t timestamp_us{0};
    /// now_us() at the write completion that put the state on the wire
    int64_t bound_us{0};
};

/// One image the artwork transfers sent in full: its announce and every part (spec
/// roles/artwork/v1.md "Artwork (Binary)")
struct SentArtwork {
    size_t channel{0};
    std::vector<uint8_t> bytes;
};

/// What the trace showed of one connection. Every JSON message is kept as the text that crossed
/// the transport.
struct ConnectionRecord {
    uint64_t connection_id{0};
    std::vector<WireEvent> events;
    std::optional<std::string> client_hello;
    /// The first server/activate sent
    std::optional<std::string> activation;
    /// The active_roles the server/activate messages sent so far leave in force
    std::vector<std::string> active_roles;
    /// Every group/update sent after the first server/activate
    std::vector<std::string> group_updates;
    std::vector<AvailabilityEntry> availability_trace;
    std::vector<TimeEntry> time_exchange;
    /// The player object of the last stream/start sent that carried one
    std::optional<std::string> stream_player;
    /// The artwork object of the last stream/start sent that carried one
    std::optional<std::string> stream_artwork;
    /// The `format` of the last client/state player object that carried one
    std::optional<std::string> preferred_format;
    /// The `available` of the last client/state received; nullopt before one carried a boolean
    std::optional<bool> latest_available;
    /// The timestamp of each audio chunk sent, in order
    std::vector<int64_t> chunk_timestamps_us;
    std::vector<SentFrame> sent_frames;
    /// A stream/end was sent and its write completed
    bool stream_end_written{false};
    /// The metadata object of the last server/state written that carried one
    std::optional<std::string> metadata;
    std::optional<FirstMetadataState> first_metadata_state;
    /// The controller object of the last server/state written that carried one
    std::optional<std::string> controller_state;
    /// The controller object of the first client/command received
    std::optional<std::string> client_command;
    /// Every artwork transfer completed and written, in order
    std::vector<SentArtwork> artwork_images;
};

/// Builds the summary evidence from ServerConfig::trace, which reports each message's plaintext
/// (inbound after decryption, outbound immediately before encryption) and, with
/// ServerConfig::trace_writes, each completed write. A message the server sent counts as sent
/// only once a later write completion covers it.
class TraceRecorder {
public:
    /// The ServerConfig::trace callback
    void on_message(const sendspin::server::TracedMessage& message);

    /// Waits until pred holds for the case connection, the latest to carry a client/hello.
    /// @return false at the deadline
    bool wait_until(Deadline deadline, const std::function<bool(const ConnectionRecord&)>& pred);

    /// @return the case connection as the summary reports it: entries whose write was never
    /// confirmed are left out, and `other-sent` entries are merged as the contract allows.
    /// nullopt before any connection carried a client/hello.
    std::optional<ConnectionRecord> snapshot() const;

private:
    /// Where an outbound message's entries go: the lists' sizes when its first frame was traced
    struct Position {
        size_t availability{0};
        size_t time_exchange{0};
        size_t events{0};
    };

    struct Connection {
        ConnectionRecord record;
        /// Time-exchange entries not yet covered by a write completion, by index
        std::vector<bool> time_confirmed;
        /// Roles of each stream/start traced and not yet covered by a write completion
        std::vector<std::vector<std::string>> unwritten_stream_starts;
        bool stream_end_traced{false};
        /// server/state metadata and controller objects traced and not yet covered by a write
        /// completion
        std::vector<std::string> unwritten_metadata;
        std::vector<std::string> unwritten_controller;
        /// The artwork transfer in flight, and the transfers completed and not yet written
        std::optional<SentArtwork> artwork_transfer;
        size_t artwork_total_size{0};
        std::vector<SentArtwork> unwritten_artwork;
        /// An outbound fragmented message being reassembled
        std::optional<uint8_t> fragment_type;
        std::vector<uint8_t> fragment_data;
        Position fragment_position;
    };

    void on_written(Connection& connection, int64_t stamp_us);
    void on_inbound(Connection& connection, bool binary, uint8_t message_id,
                    const std::vector<uint8_t>& plaintext);
    void on_outbound(Connection& connection, const sendspin::server::TracedMessage& message);
    void on_outbound_json(Connection& connection, const std::string& text,
                          const Position& position, bool binary);
    static void on_outbound_artwork(Connection& connection, const std::vector<uint8_t>& plaintext);
    static Position position_of(const Connection& connection);

    mutable std::mutex mutex_;
    std::condition_variable changed_;
    std::map<uint64_t, Connection> connections_;
    std::optional<uint64_t> case_connection_;
};

}  // namespace conformance
