#include "trace_recorder.h"

#include <ArduinoJson.h>

#include <algorithm>

namespace conformance {

namespace {

using sendspin::server::TracedMessage;
using sendspin::server::TraceDirection;

// spec messaging.md "Binary Message ID Structure" and "Fragmentation"
constexpr uint8_t JSON_MESSAGE_ID = 0;
constexpr uint8_t FRAGMENT_MESSAGE_ID = 1;
constexpr uint8_t AUDIO_CHUNK_MESSAGE_ID = 4;
constexpr uint8_t FIRST_FRAGMENT_FLAG = 0x02;
constexpr uint8_t LAST_FRAGMENT_FLAG = 0x01;
// Spans the player audio chunk header (type, int64 timestamp, uint32 send_ahead)
constexpr size_t FRAME_PREFIX_BYTES = 13;
constexpr size_t TIMESTAMP_BYTES = 8;

std::string hex_prefix(const uint8_t* data, size_t size) {
    static const char HEX[] = "0123456789abcdef";
    std::string out;
    for (size_t i = 0; i < std::min(size, FRAME_PREFIX_BYTES); ++i) {
        out.push_back(HEX[data[i] >> 4]);
        out.push_back(HEX[data[i] & 0x0F]);
    }
    return out;
}

int64_t read_be_int64(const uint8_t* data) {
    uint64_t value = 0;
    for (size_t i = 0; i < TIMESTAMP_BYTES; ++i) {
        value = (value << 8) | data[i];
    }
    return static_cast<int64_t>(value);
}

std::string serialized(JsonVariantConst value) {
    std::string out;
    serializeJson(value, out);
    return out;
}

}  // namespace

void TraceRecorder::on_message(const TracedMessage& message) {
    std::lock_guard<std::mutex> lock(this->mutex_);
    Connection& connection = this->connections_[message.connection_id];
    connection.record.connection_id = message.connection_id;
    if (message.written) {
        this->on_written(connection);
    } else if (message.direction == TraceDirection::INBOUND) {
        const uint8_t id =
            message.binary && !message.plaintext.empty() ? message.plaintext[0] : JSON_MESSAGE_ID;
        this->on_inbound(connection, message.binary, id, message.plaintext);
    } else {
        this->on_outbound(connection, message);
    }
    this->changed_.notify_all();
}

bool TraceRecorder::wait_until(Deadline deadline,
                               const std::function<bool(const ConnectionRecord&)>& pred) {
    std::unique_lock<std::mutex> lock(this->mutex_);
    return this->changed_.wait_until(lock, deadline, [&] {
        return this->case_connection_.has_value() &&
               pred(this->connections_.at(*this->case_connection_).record);
    });
}

std::optional<ConnectionRecord> TraceRecorder::snapshot() const {
    std::lock_guard<std::mutex> lock(this->mutex_);
    if (!this->case_connection_.has_value()) {
        return std::nullopt;
    }
    const Connection& connection = this->connections_.at(*this->case_connection_);
    ConnectionRecord record = connection.record;
    record.time_exchange.clear();
    for (size_t i = 0; i < connection.record.time_exchange.size(); ++i) {
        const TimeEntry& entry = connection.record.time_exchange[i];
        if (!connection.time_confirmed[i]) {
            continue;
        }
        // One other-sent stands for every send between the same two neighbours, and one with
        // nothing before it says nothing about a client/time.
        if (entry.kind == TimeEntry::Kind::OTHER_SENT &&
            (record.time_exchange.empty() ||
             record.time_exchange.back().kind == TimeEntry::Kind::OTHER_SENT)) {
            continue;
        }
        record.time_exchange.push_back(entry);
    }
    return record;
}

void TraceRecorder::on_written(Connection& connection) {
    // A write completion covers every outbound message traced before it on the connection.
    std::fill(connection.time_confirmed.begin(), connection.time_confirmed.end(), true);
    for (std::vector<std::string>& roles : connection.unwritten_stream_starts) {
        connection.record.availability_trace.push_back({true, std::nullopt, true, std::move(roles)});
    }
    connection.unwritten_stream_starts.clear();
    if (connection.stream_end_traced) {
        connection.record.stream_end_written = true;
    }
}

void TraceRecorder::on_inbound(Connection& connection, bool binary, uint8_t message_id,
                               const std::vector<uint8_t>& plaintext) {
    ConnectionRecord& record = connection.record;
    if (binary && message_id != JSON_MESSAGE_ID) {
        record.events.push_back({true, binary, message_id, {}});
        return;
    }
    const size_t skip = binary ? 1 : 0;
    std::string text(plaintext.begin() + static_cast<std::ptrdiff_t>(skip), plaintext.end());
    JsonDocument doc;
    const bool parsed = !deserializeJson(doc, text);
    const std::string type = parsed ? doc["type"] | "" : "";
    record.events.push_back({true, binary, message_id, type});

    if (type == "client/hello") {
        record.client_hello = text;
        this->case_connection_ = record.connection_id;
    } else if (type == "client/state") {
        JsonVariantConst available = doc["payload"]["available"];
        const std::optional<bool> value =
            available.is<bool>() ? std::optional<bool>(available.as<bool>()) : std::nullopt;
        record.availability_trace.push_back({false, value, false, {}});
        record.latest_available = value;
        JsonVariantConst format = doc["payload"]["player"]["format"];
        if (format.is<JsonObjectConst>()) {
            record.preferred_format = serialized(format);
        }
    } else if (type == "client/time") {
        record.time_exchange.push_back({TimeEntry::Kind::CLIENT_TIME, text});
        connection.time_confirmed.push_back(true);
    }
}

void TraceRecorder::on_outbound(Connection& connection, const TracedMessage& message) {
    const std::vector<uint8_t>& plaintext = message.plaintext;
    if (!message.binary) {
        this->on_outbound_json(connection, std::string(plaintext.begin(), plaintext.end()),
                               position_of(connection), false);
        return;
    }
    if (plaintext.empty()) {
        return;
    }
    ConnectionRecord& record = connection.record;
    const uint8_t id = plaintext[0];
    if (id == JSON_MESSAGE_ID) {
        this->on_outbound_json(connection, std::string(plaintext.begin() + 1, plaintext.end()),
                               position_of(connection), true);
        return;
    }
    if (id == FRAGMENT_MESSAGE_ID && plaintext.size() >= 2) {
        const uint8_t flags = plaintext[1];
        size_t data_start = 2;
        if ((flags & FIRST_FRAGMENT_FLAG) != 0 && plaintext.size() >= 3) {
            connection.fragment_type = plaintext[2];
            connection.fragment_data.clear();
            connection.fragment_position = position_of(connection);
            data_start = 3;
        }
        connection.fragment_data.insert(connection.fragment_data.end(),
                                        plaintext.begin() + static_cast<std::ptrdiff_t>(data_start),
                                        plaintext.end());
        const std::optional<uint8_t> original = connection.fragment_type;
        // A JSON body is a text message, whatever frames carried it.
        if (original != JSON_MESSAGE_ID) {
            record.sent_frames.push_back(
                {plaintext.size(), hex_prefix(plaintext.data(), plaintext.size()), std::nullopt});
        }
        if ((flags & LAST_FRAGMENT_FLAG) == 0 || !original.has_value()) {
            return;
        }
        const std::vector<uint8_t> data = std::move(connection.fragment_data);
        connection.fragment_type.reset();
        connection.fragment_data.clear();
        if (*original == JSON_MESSAGE_ID) {
            this->on_outbound_json(connection, std::string(data.begin(), data.end()),
                                   connection.fragment_position, true);
            return;
        }
        record.events.insert(
            record.events.begin() + static_cast<std::ptrdiff_t>(connection.fragment_position.events),
            {false, true, *original, {}});
        if (*original == AUDIO_CHUNK_MESSAGE_ID && data.size() >= TIMESTAMP_BYTES) {
            record.chunk_timestamps_us.push_back(read_be_int64(data.data()));
        }
        return;
    }
    record.events.push_back({false, true, id, {}});
    record.sent_frames.push_back(
        {plaintext.size(), hex_prefix(plaintext.data(), plaintext.size()),
         id == AUDIO_CHUNK_MESSAGE_ID ? std::optional<size_t>(message.audio_bytes) : std::nullopt});
    if (id == AUDIO_CHUNK_MESSAGE_ID && plaintext.size() >= 1 + TIMESTAMP_BYTES) {
        record.chunk_timestamps_us.push_back(read_be_int64(plaintext.data() + 1));
    }
}

void TraceRecorder::on_outbound_json(Connection& connection, const std::string& text,
                                     const Position& position, bool binary) {
    ConnectionRecord& record = connection.record;
    JsonDocument doc;
    const bool parsed = !deserializeJson(doc, text);
    const std::string type = parsed ? doc["type"] | "" : "";
    record.events.insert(record.events.begin() + static_cast<std::ptrdiff_t>(position.events),
                         {false, binary, JSON_MESSAGE_ID, type});

    // Placed where the send began, and reported once a write completion confirms it
    const auto time_at = static_cast<std::ptrdiff_t>(position.time_exchange);
    record.time_exchange.insert(record.time_exchange.begin() + time_at,
                                type == "server/time"
                                    ? TimeEntry{TimeEntry::Kind::SERVER_TIME, text}
                                    : TimeEntry{TimeEntry::Kind::OTHER_SENT, {}});
    connection.time_confirmed.insert(connection.time_confirmed.begin() + time_at, false);

    if (type == "server/activate") {
        if (!record.activation.has_value()) {
            record.activation = text;
        }
    } else if (type == "group/update") {
        if (record.activation.has_value()) {
            record.group_updates.push_back(text);
        }
    } else if (type == "stream/start") {
        std::vector<std::string> roles;
        for (JsonPairConst role : doc["payload"].as<JsonObjectConst>()) {
            if (role.value().is<JsonObjectConst>()) {
                roles.emplace_back(role.key().c_str());
            }
        }
        std::sort(roles.begin(), roles.end());
        record.availability_trace.insert(
            record.availability_trace.begin() + static_cast<std::ptrdiff_t>(position.availability),
            {true, std::nullopt, false, roles});
        connection.unwritten_stream_starts.push_back(std::move(roles));
        JsonVariantConst player = doc["payload"]["player"];
        if (player.is<JsonObjectConst>()) {
            record.stream_player = serialized(player);
        }
    } else if (type == "stream/end") {
        connection.stream_end_traced = true;
    }
}

TraceRecorder::Position TraceRecorder::position_of(const Connection& connection) {
    return {connection.record.availability_trace.size(), connection.record.time_exchange.size(),
            connection.record.events.size()};
}

}  // namespace conformance
