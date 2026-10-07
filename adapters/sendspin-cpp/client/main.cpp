#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "sendspin/artwork_role.h"
#include "sendspin/client.h"
#include "sendspin/controller_role.h"
#include "sendspin/metadata_role.h"
#include "sendspin/player_role.h"

#include <ArduinoJson.h>
#include <openssl/evp.h>

using namespace sendspin;

// sendspin-cpp decrypts and consumes audio chunks inside the library and exposes no public
// hook for them. PlayerRoleListener::on_audio_write() is the synchronized playback output,
// with priming and hard-sync silence added and late chunks dropped, so it is not the audio
// the server transported and cannot stand in for it.
static const char* const AUDIO_NOT_OBSERVABLE_REASON =
    "sendspin-cpp exposes no public hook for transported audio chunks, so the adapter cannot "
    "report the received audio hashes";

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

struct PeerInfo {
    std::string server_id;
    std::string server_name;
};

struct SessionState {
    mutable std::mutex mu;
    // The id the client presents on the wire, derived by the library from its identity.
    std::string client_id;
    std::optional<PeerInfo> peer;
    std::optional<ServerPlayerStreamObject> stream;

    int metadata_update_count{0};
    std::optional<ServerMetadataStateObject> metadata;

    std::optional<ServerStateControllerObject> controller_state;
    std::optional<std::string> sent_controller_command;

    int artwork_channel{-1};
    int artwork_count{0};
    Sha256Hasher artwork_hasher;
    size_t artwork_byte_count{0};
};

static bool is_player_scenario(const std::string& id) {
    return id == "client-initiated-pcm" || id == "server-initiated-pcm" ||
           id == "server-initiated-flac" || id == "server-initiated-opus" ||
           id == "server-initiated-pcm-24bit" || id == "server-initiated-legacy-unencrypted";
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
    return std::nullopt;
}

static const char* codec_name(SendspinCodecFormat codec) {
    switch (codec) {
        case SendspinCodecFormat::FLAC:
            return "flac";
        case SendspinCodecFormat::OPUS:
            return "opus";
        case SendspinCodecFormat::PCM:
            return "pcm";
        case SendspinCodecFormat::UNSUPPORTED:
            break;
    }
    return "unsupported";
}

static const char* repeat_mode_name(SendspinRepeatMode mode) {
    switch (mode) {
        case SendspinRepeatMode::OFF:
            return "off";
        case SendspinRepeatMode::ONE:
            return "one";
        case SendspinRepeatMode::ALL:
            return "all";
    }
    return "off";
}

// Wire names of the controller commands, indexed by SendspinControllerCommand.
static constexpr const char* CONTROLLER_COMMAND_NAMES[] = {
    "play",       "pause",      "stop",    "next",      "previous", "volume", "mute",
    "repeat_off", "repeat_one", "repeat_all", "shuffle", "unshuffle", "switch", "seek",
    "seek_relative",
};

static const char* controller_command_name(SendspinControllerCommand command) {
    const auto index = static_cast<size_t>(command);
    return index < std::size(CONTROLLER_COMMAND_NAMES) ? CONTROLLER_COMMAND_NAMES[index]
                                                       : "unknown";
}

static std::optional<SendspinControllerCommand> controller_command_from_name(
    const std::string& name) {
    for (size_t i = 0; i < std::size(CONTROLLER_COMMAND_NAMES); i++) {
        if (name == CONTROLLER_COMMAND_NAMES[i]) {
            return static_cast<SendspinControllerCommand>(i);
        }
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
        auto target = controller_command_from_name(target_command_);
        if (!target.has_value()) {
            return;
        }
        bool supported = std::find(controller.supported_commands.begin(),
                                   controller.supported_commands.end(),
                                   target.value()) != controller.supported_commands.end();
        if (!supported) {
            return;
        }
        // true means queued for the protocol task, which is as far as the library reports.
        if (!controller_.send_command({.command = target.value()})) {
            return;
        }
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

// Stops the client when the session scope exits. Declared after the listeners and the
// network provider so the library's threads are joined while those are still alive.
struct StopOnExit {
    SendspinClient& client;
    ~StopOnExit() {
        client.stop();
    }
};

static SendspinClientConfig build_client_config(const Args& args) {
    SendspinClientConfig config;
    config.name = args.client_name;
    config.product_name = "sendspin-cpp Conformance Client";
    config.manufacturer = "Sendspin Conformance";
    config.software_version = "0.2.0";
    // The harness hands every case its own client listener port so parallel cases do not
    // fight over one socket; leaving this at DEFAULT_SERVER_PORT would bind 8928 for all.
    config.server_port = static_cast<uint16_t>(args.port);
    return config;
}

static PlayerRoleConfig build_player_config(const Args& args) {
    PlayerRoleConfig config;
    if (is_player_scenario(args.scenario_id)) {
        SendspinCodecFormat codec = SendspinCodecFormat::PCM;
        if (args.preferred_codec == "flac") {
            codec = SendspinCodecFormat::FLAC;
        } else if (args.preferred_codec == "opus") {
            codec = SendspinCodecFormat::OPUS;
        }
        // The 24-bit scenario hinges on a client that advertises 24-bit as its only
        // supported depth; advertising 16-bit here would have the server stream 16-bit
        // and the case would pass without exercising the 24-bit path at all.
        const uint8_t bit_depth =
            args.scenario_id == "server-initiated-pcm-24bit" ? static_cast<uint8_t>(24)
                                                            : static_cast<uint8_t>(16);
        AudioSupportedFormatObject format{
            codec,
            1,
            8000,
            bit_depth,
        };
        config.audio_formats = {format};
    }
    return config;
}

static ArtworkRoleConfig build_artwork_config(const Args& args) {
    ArtworkRoleConfig config;
    config.preferred_formats = {
        ImageSlotPreference{
            SendspinImageSource::ALBUM,
            parse_image_format(args.artwork_format).value_or(SendspinImageFormat::JPEG),
            static_cast<uint16_t>(args.artwork_width),
            static_cast<uint16_t>(args.artwork_height),
        },
    };
    return config;
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

    std::lock_guard<std::mutex> lock(state.mu);

    if (state.client_id.empty()) {
        doc["client_id"] = nullptr;
    } else {
        doc["client_id"] = state.client_id;
    }

    if (state.peer.has_value()) {
        auto server = doc["server"].to<JsonObject>();
        server["server_id"] = state.peer->server_id;
        server["name"] = state.peer->server_name;
        server["version"] = 1;
        server["connection_reason"] = nullptr;
    } else {
        doc["server"] = nullptr;
    }

    if (state.stream.has_value()) {
        auto stream = doc["stream"].to<JsonObject>();
        if (state.stream->codec.has_value()) {
            stream["codec"] = codec_name(state.stream->codec.value());
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
        audio["audio_chunk_count"] = nullptr;
        audio["received_encoded_sha256"] = nullptr;
        audio["received_pcm_sha256"] = nullptr;
        audio["received_sample_count"] = nullptr;
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
                commands.add(controller_command_name(command));
            }
            received["volume"] = state.controller_state->volume;
            received["muted"] = state.controller_state->muted;
            received["repeat"] = repeat_mode_name(state.controller_state->repeat);
            received["shuffle"] = state.controller_state->shuffle;
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
    SendspinClientConfig config = build_client_config(args);
    SendspinClient client(std::move(config));
    AlwaysReadyNetworkProvider network_provider;
    client.set_network_provider(&network_provider);
    // The adapter does not derive the harness's deterministic pairing credentials, so the
    // client is unpaired with every server and takes part only under unpaired access.
    client.set_unpaired_access_enabled(true);

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

    // start() reports role startup, not the listener bind: a failure to bind is retried
    // by the library rather than returned.
    if (!client.start()) {
        SessionState empty;
        return emit_summary(args, empty, "error", "Failed to start client roles");
    }
    StopOnExit stop_on_exit{client};
    state.client_id = client.client_id();
    client.loop();
    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    if (args.initiator_role == "client") {
        if (!connect_url.has_value()) {
            SessionState empty;
            return emit_summary(args, empty, "error", "No transport path was configured");
        }
        client.connect_to(connect_url.value());
    }

    if (args.initiator_role == "server") {
        const std::string local_url =
            "ws://127.0.0.1:" + std::to_string(args.port) + args.path;
        register_endpoint(args.registry, args.client_name, local_url);
        write_ready_file(args, local_url);
    }

    auto deadline = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(int64_t(args.timeout_seconds * 1000));
    bool had_handshake = false;
    bool disconnected_after_handshake = false;

    while (std::chrono::steady_clock::now() < deadline) {
        client.loop();

        if (client.is_connected()) {
            had_handshake = true;
            auto server_info = client.get_server_information();
            PeerInfo peer{
                server_info.has_value() && !server_info->server_id.empty()
                    ? server_info->server_id
                    : args.server_id,
                server_info.has_value() && !server_info->name.empty() ? server_info->name
                                                                      : args.server_name,
            };
            std::lock_guard<std::mutex> lock(state.mu);
            state.peer = std::move(peer);
        } else if (had_handshake) {
            disconnected_after_handshake = true;
            break;
        }

        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    if (disconnected_after_handshake) {
        for (int i = 0; i < 10; i++) {
            client.loop();
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        if (is_player_scenario(args.scenario_id)) {
            return emit_summary(args, state, "error", AUDIO_NOT_OBSERVABLE_REASON);
        }
        return emit_summary(args, state, "ok", "");
    }

    if (!had_handshake) {
        return emit_summary(args, state, "error", "Timed out waiting for server connection");
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
