#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#define private public
#include "sendspin/client.h"
#undef private

#include <ArduinoJson.h>
#include <openssl/evp.h>

#include "connection.h"
#include "connection_manager.h"

using namespace sendspin;

static constexpr size_t BINARY_HEADER_SIZE = 9;

struct Args {
    std::string client_name;
    std::string client_id;
    std::string summary;
    std::string ready;
    std::string registry;
    std::string scenario_id = "client-initiated-pcm";
    std::string initiator_role = "client";
    std::string preferred_codec = "pcm";
    std::string server_name = "Sendspin Conformance Server";
    std::string server_id = "conformance-server";
    double timeout_seconds = 30.0;
    int port = 8928;
    std::string path = "/sendspin";
    std::string log_level = "info";
    std::string metadata_title = "Almost Silent";
    std::string metadata_artist = "Sendspin Conformance";
    std::string metadata_album_artist = "Sendspin";
    std::string metadata_album = "Protocol Fixtures";
    std::string metadata_artwork_url = "https://example.invalid/almost-silent.jpg";
    int metadata_year = 2026;
    int metadata_track = 1;
    std::string metadata_repeat = "all";
    std::string metadata_shuffle = "false";
    int metadata_track_progress = 12000;
    int metadata_track_duration = 180000;
    int metadata_playback_speed = 1000;
    std::string controller_command = "next";
    std::string artwork_format = "jpeg";
    int artwork_width = 256;
    int artwork_height = 256;
};

class Sha256Hasher {
public:
    Sha256Hasher() : ctx_(EVP_MD_CTX_new()) {
        if (ctx_ == nullptr || EVP_DigestInit_ex(ctx_, EVP_sha256(), nullptr) != 1) {
            throw std::runtime_error("Failed to initialize SHA-256 context");
        }
    }

    Sha256Hasher(const Sha256Hasher&) = delete;
    Sha256Hasher& operator=(const Sha256Hasher&) = delete;

    ~Sha256Hasher() {
        if (ctx_ != nullptr) {
            EVP_MD_CTX_free(ctx_);
        }
    }

    void update(const uint8_t* data, size_t len) {
        if (EVP_DigestUpdate(ctx_, data, len) != 1) {
            throw std::runtime_error("Failed to update SHA-256 digest");
        }
    }

    std::string hexdigest() const {
        EVP_MD_CTX* copy = EVP_MD_CTX_new();
        if (copy == nullptr) {
            throw std::runtime_error("Failed to clone SHA-256 context");
        }
        unsigned char hash[EVP_MAX_MD_SIZE];
        unsigned int hash_len = 0;
        std::string digest_hex;
        if (EVP_MD_CTX_copy_ex(copy, ctx_) != 1 ||
            EVP_DigestFinal_ex(copy, hash, &hash_len) != 1) {
            EVP_MD_CTX_free(copy);
            throw std::runtime_error("Failed to finalize SHA-256 digest");
        }
        digest_hex = hex_lower(hash, hash_len);
        EVP_MD_CTX_free(copy);
        return digest_hex;
    }

private:
    static std::string hex_lower(const unsigned char* data, size_t len) {
        static const char hex[] = "0123456789abcdef";
        std::string out;
        out.reserve(len * 2);
        for (size_t i = 0; i < len; i++) {
            out.push_back(hex[data[i] >> 4]);
            out.push_back(hex[data[i] & 0x0F]);
        }
        return out;
    }

    EVP_MD_CTX* ctx_;
};

class FloatPcmHasher {
public:
    void update_from_pcm_bytes(const uint8_t* data, size_t len, int bit_depth) {
        if (bit_depth == 16) {
            for (size_t i = 0; i + 1 < len; i += 2) {
                int16_t sample =
                    int16_t(uint16_t(data[i]) | (uint16_t(data[i + 1]) << 8));
                push_sample(float(sample) / 32768.0f);
            }
        } else if (bit_depth == 24) {
            for (size_t i = 0; i + 2 < len; i += 3) {
                int32_t value =
                    int32_t(data[i]) | (int32_t(data[i + 1]) << 8) |
                    (int32_t(data[i + 2]) << 16);
                if (value & 0x800000) {
                    value |= ~0x00FFFFFF;
                }
                push_sample(float(value) / 8388608.0f);
            }
        } else if (bit_depth == 32) {
            for (size_t i = 0; i + 3 < len; i += 4) {
                int32_t sample = int32_t(uint32_t(data[i]) | (uint32_t(data[i + 1]) << 8) |
                                         (uint32_t(data[i + 2]) << 16) |
                                         (uint32_t(data[i + 3]) << 24));
                push_sample(float(sample) / 2147483648.0f);
            }
        }
    }

    std::string hexdigest() const {
        return hasher_.hexdigest();
    }

    size_t sample_count() const {
        return sample_count_;
    }

private:
    void push_sample(float sample) {
        uint8_t bytes[4];
        std::memcpy(bytes, &sample, sizeof(bytes));
        hasher_.update(bytes, sizeof(bytes));
        sample_count_++;
    }

    Sha256Hasher hasher_;
    size_t sample_count_{0};
};

struct PeerInfo {
    std::string server_id;
    std::string server_name;
    std::string connection_reason;
};

struct SessionState {
    mutable std::mutex mu;
    std::optional<PeerInfo> peer;
    std::optional<ServerPlayerStreamObject> stream;

    FloatPcmHasher pcm_hasher;
    Sha256Hasher encoded_hasher;
    size_t received_sample_count{0};
    int audio_chunk_count{0};
    bool hash_pcm_from_payload{false};

    int metadata_update_count{0};
    std::optional<ServerMetadataStateObject> metadata;

    std::optional<ServerStateControllerObject> controller_state;
    std::optional<std::string> sent_controller_command;

    int artwork_channel{-1};
    int artwork_count{0};
    Sha256Hasher artwork_hasher;
    size_t artwork_byte_count{0};

    SendspinConnection* hooked_connection{nullptr};
};

static bool is_player_scenario(const std::string& id) {
    return id == "client-initiated-pcm" || id == "server-initiated-pcm" ||
           id == "server-initiated-flac" || id == "server-initiated-pcm-24bit";
}

static bool is_metadata_scenario(const std::string& id) {
    return id == "server-initiated-metadata";
}

static bool is_controller_scenario(const std::string& id) {
    return id == "server-initiated-controller";
}

static bool is_artwork_scenario(const std::string& id) {
    return id == "server-initiated-artwork";
}

static std::string get_arg(int argc, char* argv[], const std::string& name,
                           const std::string& def = "") {
    std::string flag = "--" + name;
    for (int i = 1; i < argc - 1; i++) {
        if (argv[i] == flag) {
            return argv[i + 1];
        }
    }
    return def;
}

static int get_int_arg(int argc, char* argv[], const std::string& name, int def) {
    std::string val = get_arg(argc, argv, name, "");
    return val.empty() ? def : std::stoi(val);
}

static double get_double_arg(int argc, char* argv[], const std::string& name, double def) {
    std::string val = get_arg(argc, argv, name, "");
    return val.empty() ? def : std::stod(val);
}

static Args parse_args(int argc, char* argv[]) {
    Args a;
    a.client_name = get_arg(argc, argv, "client-name");
    a.client_id = get_arg(argc, argv, "client-id");
    a.summary = get_arg(argc, argv, "summary");
    a.ready = get_arg(argc, argv, "ready");
    a.registry = get_arg(argc, argv, "registry");
    a.scenario_id = get_arg(argc, argv, "scenario-id", a.scenario_id);
    a.initiator_role = get_arg(argc, argv, "initiator-role", a.initiator_role);
    a.preferred_codec = get_arg(argc, argv, "preferred-codec", a.preferred_codec);
    a.server_name = get_arg(argc, argv, "server-name", a.server_name);
    a.server_id = get_arg(argc, argv, "server-id", a.server_id);
    a.timeout_seconds = get_double_arg(argc, argv, "timeout-seconds", a.timeout_seconds);
    a.port = get_int_arg(argc, argv, "port", a.port);
    a.path = get_arg(argc, argv, "path", a.path);
    a.log_level = get_arg(argc, argv, "log-level", a.log_level);
    a.metadata_title = get_arg(argc, argv, "metadata-title", a.metadata_title);
    a.metadata_artist = get_arg(argc, argv, "metadata-artist", a.metadata_artist);
    a.metadata_album_artist =
        get_arg(argc, argv, "metadata-album-artist", a.metadata_album_artist);
    a.metadata_album = get_arg(argc, argv, "metadata-album", a.metadata_album);
    a.metadata_artwork_url =
        get_arg(argc, argv, "metadata-artwork-url", a.metadata_artwork_url);
    a.metadata_year = get_int_arg(argc, argv, "metadata-year", a.metadata_year);
    a.metadata_track = get_int_arg(argc, argv, "metadata-track", a.metadata_track);
    a.metadata_repeat = get_arg(argc, argv, "metadata-repeat", a.metadata_repeat);
    a.metadata_shuffle = get_arg(argc, argv, "metadata-shuffle", a.metadata_shuffle);
    a.metadata_track_progress =
        get_int_arg(argc, argv, "metadata-track-progress", a.metadata_track_progress);
    a.metadata_track_duration =
        get_int_arg(argc, argv, "metadata-track-duration", a.metadata_track_duration);
    a.metadata_playback_speed =
        get_int_arg(argc, argv, "metadata-playback-speed", a.metadata_playback_speed);
    a.controller_command = get_arg(argc, argv, "controller-command", a.controller_command);
    a.artwork_format = get_arg(argc, argv, "artwork-format", a.artwork_format);
    a.artwork_width = get_int_arg(argc, argv, "artwork-width", a.artwork_width);
    a.artwork_height = get_int_arg(argc, argv, "artwork-height", a.artwork_height);
    return a;
}

static void write_json_file(const std::string& path, const JsonDocument& doc) {
    std::ofstream out(path);
    serializeJson(doc, out);
}

static void register_endpoint(const std::string& registry_path, const std::string& name,
                              const std::string& url) {
    JsonDocument doc;
    {
        std::ifstream in(registry_path);
        if (in.good()) {
            deserializeJson(doc, in);
        }
    }
    doc[name]["url"] = url;
    write_json_file(registry_path, doc);
}

static std::string wait_for_server_url(const std::string& registry_path,
                                       const std::string& server_name, double timeout_seconds) {
    auto deadline = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(int64_t(timeout_seconds * 1000));
    while (std::chrono::steady_clock::now() < deadline) {
        JsonDocument doc;
        std::ifstream in(registry_path);
        if (in.good() && deserializeJson(doc, in) == DeserializationError::Ok) {
            const char* url = doc[server_name]["url"];
            if (url && url[0] != '\0') {
                return url;
            }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return {};
}

static std::optional<LogLevel> parse_log_level(const std::string& raw) {
    if (raw == "none") {
        return LogLevel::NONE;
    }
    if (raw == "error") {
        return LogLevel::ERROR;
    }
    if (raw == "warn") {
        return LogLevel::WARN;
    }
    if (raw == "info") {
        return LogLevel::INFO;
    }
    if (raw == "debug") {
        return LogLevel::DEBUG;
    }
    if (raw == "verbose") {
        return LogLevel::VERBOSE;
    }
    return std::nullopt;
}

static std::optional<SendspinImageFormat> parse_image_format(const std::string& raw) {
    if (raw == "jpeg") {
        return SendspinImageFormat::JPEG;
    }
    if (raw == "png") {
        return SendspinImageFormat::PNG;
    }
    if (raw == "bmp") {
        return SendspinImageFormat::BMP;
    }
    return std::nullopt;
}

static JsonObject add_optional_string(JsonObject parent, const char* key,
                                      const std::optional<std::string>& value) {
    if (value.has_value()) {
        parent[key] = value.value();
    } else {
        parent[key] = nullptr;
    }
    return parent;
}

static void write_ready_file(const Args& args, const std::optional<std::string>& url = std::nullopt) {
    JsonDocument ready_doc;
    ready_doc["status"] = "ready";
    ready_doc["scenario_id"] = args.scenario_id;
    ready_doc["initiator_role"] = args.initiator_role;
    if (url.has_value()) {
        ready_doc["url"] = url.value();
    }
    write_json_file(args.ready, ready_doc);
}

class AlwaysReadyNetworkProvider : public SendspinNetworkProvider {
public:
    bool is_network_ready() override {
        return true;
    }
};

class HashingPlayerListener : public PlayerRoleListener {
public:
    HashingPlayerListener(SessionState& state, PlayerRole& player) : state_(state), player_(player) {}

    size_t on_audio_write(uint8_t* /*data*/, size_t length, uint32_t /*timeout_ms*/) override {
        // Drain the player's synchronized playback buffer so the pipeline keeps
        // flowing, but do not hash it: this buffer is the output of the real-time
        // sync task, which injects initial-sync silence and soft-sync sample
        // interpolation, so it is not byte-identical to the transported PCM.
        // The canonical PCM hash is computed from the wire payload instead (see
        // install_binary_observer), where PCM frames are carried verbatim.
        return length;
    }

    void on_stream_start() override {
        std::lock_guard<std::mutex> lock(state_.mu);
        state_.stream = player_.get_current_stream_params();
    }

private:
    SessionState& state_;
    PlayerRole& player_;
};

class HashingMetadataListener : public MetadataRoleListener {
public:
    explicit HashingMetadataListener(SessionState& state) : state_(state) {}

    void on_metadata(const ServerMetadataStateObject& metadata) override {
        std::lock_guard<std::mutex> lock(state_.mu);
        state_.metadata_update_count++;
        state_.metadata = metadata;
    }

private:
    SessionState& state_;
};

class HashingControllerListener : public ControllerRoleListener {
public:
    HashingControllerListener(SessionState& state, ControllerRole& controller,
                              const std::string& target_command)
        : state_(state), controller_(controller), target_command_(target_command) {}

    void on_controller_state(const ServerStateControllerObject& controller) override {
        if (controller.supported_commands.empty()) {
            return;
        }
        std::lock_guard<std::mutex> lock(state_.mu);
        state_.controller_state = controller;
        if (command_sent_) {
            return;
        }
        auto target = controller_command_from_string(target_command_);
        if (!target.has_value()) {
            return;
        }
        bool supported = std::find(controller.supported_commands.begin(),
                                   controller.supported_commands.end(),
                                   target.value()) != controller.supported_commands.end();
        if (!supported) {
            return;
        }
        controller_.send_command(target.value());
        state_.sent_controller_command = target_command_;
        command_sent_ = true;
    }

private:
    SessionState& state_;
    ControllerRole& controller_;
    std::string target_command_;
    bool command_sent_{false};
};

class HashingArtworkListener : public ArtworkRoleListener {
public:
    explicit HashingArtworkListener(SessionState& state) : state_(state) {}

    void on_image_decode(uint8_t slot, const uint8_t* data, size_t len,
                         SendspinImageFormat /*format*/) override {
        if (data == nullptr || len == 0) {
            return;
        }
        std::lock_guard<std::mutex> lock(state_.mu);
        state_.artwork_channel = slot;
        state_.artwork_count++;
        state_.artwork_byte_count += len;
        state_.artwork_hasher.update(data, len);
    }

private:
    SessionState& state_;
};

static SendspinClientConfig build_client_config(const Args& args) {
    SendspinClientConfig config;
    config.client_id = args.client_id;
    config.name = args.client_name;
    config.product_name = "sendspin-cpp Conformance Client";
    config.manufacturer = "Sendspin Conformance";
    config.software_version = "0.2.0";
    return config;
}

static PlayerRoleConfig build_player_config(const Args& args) {
    PlayerRoleConfig config;
    if (is_player_scenario(args.scenario_id)) {
        AudioSupportedFormatObject format{
            args.preferred_codec == "flac" ? SendspinCodecFormat::FLAC : SendspinCodecFormat::PCM,
            1,
            8000,
            16,
        };
        config.audio_formats = {format};
    }
    return config;
}

static ArtworkRoleConfig build_artwork_config(const Args& args) {
    ArtworkRoleConfig config;
    config.preferred_formats = {
        ImageSlotPreference{
            0,
            SendspinImageSource::ALBUM,
            parse_image_format(args.artwork_format).value_or(SendspinImageFormat::JPEG),
            static_cast<uint16_t>(args.artwork_width),
            static_cast<uint16_t>(args.artwork_height),
        },
    };
    return config;
}

static SendspinConnection* current_connection(SendspinClient& client) {
    if (client.connection_manager_ == nullptr) {
        return nullptr;
    }
    return client.connection_manager_->current();
}

static void install_binary_observer(SendspinClient& client, SessionState& state) {
    SendspinConnection* conn = current_connection(client);
    if (conn == nullptr || conn == state.hooked_connection) {
        return;
    }
    auto original_binary = conn->on_binary_message_cb;
    conn->on_binary_message_cb =
        [&state, original_binary](SendspinConnection* current, uint8_t* payload, size_t len) {
            if (payload != nullptr && len > BINARY_HEADER_SIZE &&
                payload[0] == SENDSPIN_BINARY_PLAYER_AUDIO) {
                const uint8_t* audio = payload + BINARY_HEADER_SIZE;
                size_t audio_len = len - BINARY_HEADER_SIZE;
                std::lock_guard<std::mutex> lock(state.mu);
                state.audio_chunk_count++;
                state.encoded_hasher.update(audio, audio_len);
                if (state.hash_pcm_from_payload) {
                    // For PCM the wire payload carries decoded samples verbatim, so
                    // hashing it reproduces the server's canonical PCM exactly. Use the
                    // negotiated stream bit depth when known, falling back to 16-bit.
                    int bit_depth =
                        (state.stream.has_value() && state.stream->bit_depth.has_value())
                            ? int(state.stream->bit_depth.value())
                            : 16;
                    state.pcm_hasher.update_from_pcm_bytes(audio, audio_len, bit_depth);
                    state.received_sample_count = state.pcm_hasher.sample_count();
                }
            }
            if (original_binary) {
                original_binary(current, payload, len);
            }
        };
    state.hooked_connection = conn;
}

static JsonDocument build_summary(const Args& args, const SessionState& state,
                                  const std::string& status, const std::string& reason) {
    JsonDocument doc;
    doc["status"] = status;
    if (reason.empty()) {
        doc["reason"] = nullptr;
    } else {
        doc["reason"] = reason;
    }
    doc["implementation"] = "sendspin-cpp";
    doc["role"] = "client";
    doc["scenario_id"] = args.scenario_id;
    doc["initiator_role"] = args.initiator_role;
    doc["preferred_codec"] = args.preferred_codec;
    doc["client_name"] = args.client_name;
    doc["client_id"] = args.client_id;

    std::lock_guard<std::mutex> lock(state.mu);

    if (state.peer.has_value()) {
        auto server = doc["server"].to<JsonObject>();
        server["server_id"] = state.peer->server_id;
        server["name"] = state.peer->server_name;
        server["version"] = 1;
        server["connection_reason"] = state.peer->connection_reason;
    } else {
        doc["server"] = nullptr;
    }

    if (state.stream.has_value()) {
        auto stream = doc["stream"].to<JsonObject>();
        if (state.stream->codec.has_value()) {
            stream["codec"] = to_cstr(state.stream->codec.value());
        } else {
            stream["codec"] = nullptr;
        }
        if (state.stream->sample_rate.has_value()) {
            stream["sample_rate"] = state.stream->sample_rate.value();
        } else {
            stream["sample_rate"] = nullptr;
        }
        if (state.stream->channels.has_value()) {
            stream["channels"] = state.stream->channels.value();
        } else {
            stream["channels"] = nullptr;
        }
        if (state.stream->bit_depth.has_value()) {
            stream["bit_depth"] = state.stream->bit_depth.value();
        } else {
            stream["bit_depth"] = nullptr;
        }
        add_optional_string(stream, "codec_header", state.stream->codec_header);
    } else {
        doc["stream"] = nullptr;
    }

    if (is_player_scenario(args.scenario_id)) {
        auto audio = doc["audio"].to<JsonObject>();
        audio["audio_chunk_count"] = state.audio_chunk_count;
        if (state.audio_chunk_count > 0) {
            audio["received_encoded_sha256"] = state.encoded_hasher.hexdigest();
        } else {
            audio["received_encoded_sha256"] = nullptr;
        }
        if (state.received_sample_count > 0) {
            audio["received_pcm_sha256"] = state.pcm_hasher.hexdigest();
        } else {
            audio["received_pcm_sha256"] = nullptr;
        }
        audio["received_sample_count"] = state.received_sample_count;
    } else if (is_metadata_scenario(args.scenario_id)) {
        auto metadata = doc["metadata"].to<JsonObject>();
        metadata["update_count"] = state.metadata_update_count;
        if (state.metadata.has_value()) {
            auto received = metadata["received"].to<JsonObject>();
            add_optional_string(received, "title", state.metadata->title);
            add_optional_string(received, "artist", state.metadata->artist);
            add_optional_string(received, "album_artist", state.metadata->album_artist);
            add_optional_string(received, "album", state.metadata->album);
            add_optional_string(received, "artwork_url", state.metadata->artwork_url);
            if (state.metadata->year.has_value()) {
                received["year"] = state.metadata->year.value();
            } else {
                received["year"] = nullptr;
            }
            if (state.metadata->track.has_value()) {
                received["track"] = state.metadata->track.value();
            } else {
                received["track"] = nullptr;
            }
            if (state.metadata->progress.has_value()) {
                auto progress = received["progress"].to<JsonObject>();
                progress["track_progress"] = state.metadata->progress->track_progress;
                progress["track_duration"] = state.metadata->progress->track_duration;
                progress["playback_speed"] = state.metadata->progress->playback_speed;
            } else {
                received["progress"] = nullptr;
            }
        } else {
            metadata["received"] = nullptr;
        }
    } else if (is_controller_scenario(args.scenario_id)) {
        auto controller = doc["controller"].to<JsonObject>();
        if (state.controller_state.has_value()) {
            auto received = controller["received_state"].to<JsonObject>();
            auto commands = received["supported_commands"].to<JsonArray>();
            for (const auto& command : state.controller_state->supported_commands) {
                commands.add(to_cstr(command));
            }
            received["volume"] = state.controller_state->volume;
            received["muted"] = state.controller_state->muted;
        } else {
            controller["received_state"] = nullptr;
        }
        if (state.sent_controller_command.has_value()) {
            auto sent = controller["sent_command"].to<JsonObject>();
            sent["command"] = state.sent_controller_command.value();
        } else {
            controller["sent_command"] = nullptr;
        }
    } else if (is_artwork_scenario(args.scenario_id)) {
        auto artwork = doc["artwork"].to<JsonObject>();
        if (state.artwork_channel >= 0) {
            artwork["channel"] = state.artwork_channel;
        } else {
            artwork["channel"] = nullptr;
        }
        artwork["received_count"] = state.artwork_count;
        if (state.artwork_count > 0) {
            artwork["received_sha256"] = state.artwork_hasher.hexdigest();
        } else {
            artwork["received_sha256"] = nullptr;
        }
        artwork["byte_count"] = state.artwork_byte_count;
    }

    return doc;
}

static int emit_summary(const Args& args, const SessionState& state, const std::string& status,
                        const std::string& reason) {
    auto summary = build_summary(args, state, status, reason);
    write_json_file(args.summary, summary);
    std::string out;
    serializeJson(summary, out);
    std::cout << out;
    return status == "ok" ? 0 : 1;
}

static int run_session(const Args& args, const std::optional<std::string>& connect_url) {
    auto level = parse_log_level(args.log_level).value_or(LogLevel::INFO);
    SendspinClient::set_log_level(level);

    SessionState state;
    state.hash_pcm_from_payload =
        is_player_scenario(args.scenario_id) && args.preferred_codec == "pcm";
    SendspinClientConfig config = build_client_config(args);
    SendspinClient client(std::move(config));
    AlwaysReadyNetworkProvider network_provider;
    client.set_network_provider(&network_provider);

    std::unique_ptr<HashingPlayerListener> player_listener;
    std::unique_ptr<HashingMetadataListener> metadata_listener;
    std::unique_ptr<HashingControllerListener> controller_listener;
    std::unique_ptr<HashingArtworkListener> artwork_listener;

    if (is_player_scenario(args.scenario_id)) {
        auto player_config = build_player_config(args);
        auto& player = client.add_player(std::move(player_config));
        player_listener = std::make_unique<HashingPlayerListener>(state, player);
        player.set_listener(player_listener.get());
    }

    if (is_metadata_scenario(args.scenario_id)) {
        auto& metadata = client.add_metadata();
        metadata_listener = std::make_unique<HashingMetadataListener>(state);
        metadata.set_listener(metadata_listener.get());
    }

    if (is_controller_scenario(args.scenario_id)) {
        auto& controller = client.add_controller();
        controller_listener = std::make_unique<HashingControllerListener>(
            state, controller, args.controller_command);
        controller.set_listener(controller_listener.get());
    }

    if (is_artwork_scenario(args.scenario_id)) {
        auto& artwork = client.add_artwork(build_artwork_config(args));
        artwork_listener = std::make_unique<HashingArtworkListener>(state);
        artwork.set_listener(artwork_listener.get());
    }

    if (!client.start_server()) {
        SessionState empty;
        return emit_summary(args, empty, "error", "Failed to start client listener");
    }
    client.loop();
    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    if (args.initiator_role == "client") {
        if (!connect_url.has_value()) {
            SessionState empty;
            return emit_summary(args, empty, "error", "No transport path was configured");
        }
        client.connect_to(connect_url.value());
        install_binary_observer(client, state);
    }

    const std::string local_url = "ws://127.0.0.1:8928" + args.path;
    if (args.initiator_role == "server") {
        register_endpoint(args.registry, args.client_name, local_url);
        write_ready_file(args, local_url);
    }

    auto deadline = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(int64_t(args.timeout_seconds * 1000));
    bool had_transport_connection = false;
    bool had_handshake = false;
    bool disconnected_after_handshake = false;
    bool lost_before_handshake = false;

    while (std::chrono::steady_clock::now() < deadline) {
        install_binary_observer(client, state);
        client.loop();
        install_binary_observer(client, state);

        SendspinConnection* conn = current_connection(client);
        if (conn != nullptr) {
            had_transport_connection = true;
        }

        if (client.is_connected()) {
            had_handshake = true;
            SendspinConnection* current = current_connection(client);
            if (current != nullptr) {
                auto server_info = client.get_server_information();
                PeerInfo peer{
                    server_info.has_value() && !server_info->server_id.empty()
                        ? server_info->server_id
                        : (current->get_server_id().empty() ? args.server_id : current->get_server_id()),
                    server_info.has_value() && !server_info->name.empty()
                        ? server_info->name
                        : args.server_name,
                    to_cstr(current->get_connection_reason()),
                };
                std::lock_guard<std::mutex> lock(state.mu);
                state.peer = std::move(peer);
            }
        } else if (had_handshake) {
            disconnected_after_handshake = true;
            break;
        } else if (had_transport_connection && conn == nullptr) {
            lost_before_handshake = true;
            break;
        }

        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    if (disconnected_after_handshake) {
        for (int i = 0; i < 10; i++) {
            client.loop();
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        return emit_summary(args, state, "ok", "");
    }

    if (lost_before_handshake) {
        return emit_summary(args, state, "error", "Connection closed before handshake completed");
    }

    if (!had_transport_connection) {
        if (args.initiator_role == "server") {
            return emit_summary(args, state, "error", "Timed out waiting for server connection");
        }
        return emit_summary(args, state, "error", "Timed out waiting for server connection");
    }

    if (!had_handshake) {
        return emit_summary(args, state, "error", "Timed out waiting for handshake completion");
    }

    return emit_summary(args, state, "error", "Timed out waiting for server disconnect");
}

int main(int argc, char* argv[]) {
    Args args = parse_args(argc, argv);

    if (args.initiator_role == "client") {
        write_ready_file(args);
        std::string server_url =
            wait_for_server_url(args.registry, args.server_name, args.timeout_seconds);
        if (server_url.empty()) {
            SessionState empty;
            return emit_summary(
                args,
                empty,
                "error",
                "Timed out waiting for server " + args.server_name);
        }
        return run_session(args, server_url);
    }

    return run_session(args, std::nullopt);
}
