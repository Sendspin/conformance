#include "server_session.h"

#include "json_file.h"

#include <chrono>
#include <iostream>
#include <stdexcept>
#include <thread>

namespace conformance {

namespace {

using namespace sendspin::server;

constexpr auto REGISTRY_POLL_INTERVAL = std::chrono::milliseconds(100);

const char* error_kind_name(ErrorKind kind) {
    switch (kind) {
        case ErrorKind::DIAL_FAILED:
            return "dial failed";
        case ErrorKind::DISCOVERY_UNAVAILABLE:
            return "discovery unavailable";
        case ErrorKind::NO_PLAYABLE_FORMAT:
            return "the player listed no format the server can stream";
        case ErrorKind::GOODBYE_BEFORE_ACTIVATION:
            return "client/goodbye before the client took the connection";
        case ErrorKind::HANDSHAKE_FAILED:
            return "handshake failed";
        case ErrorKind::PIPELINE_FAILED:
            return "pipeline failed";
        case ErrorKind::PERSISTENCE_FAILED:
            return "persistence failed";
        case ErrorKind::SOURCE_FAILED:
            return "source failed";
    }
    return "unknown error";
}

const char* disconnect_reason_name(DisconnectReason reason) {
    switch (reason) {
        case DisconnectReason::GOODBYE:
            return "client/goodbye";
        case DisconnectReason::CONNECTION_LOST:
            return "connection lost";
        case DisconnectReason::CLOSED_BY_SERVER:
            return "closed by the server";
        case DisconnectReason::SERVER_STOPPED:
            return "server stopped";
        case DisconnectReason::DISCONNECTED_BY_HOST:
            return "disconnected by the host";
    }
    return "unknown";
}

const char* goodbye_reason_name(GoodbyeReason reason) {
    switch (reason) {
        case GoodbyeReason::ANOTHER_SERVER:
            return "another_server";
        case GoodbyeReason::SHUTDOWN:
            return "shutdown";
        case GoodbyeReason::RESTART:
            return "restart";
        case GoodbyeReason::USER_REQUEST:
            return "user_request";
        case GoodbyeReason::UNAUTHORIZED:
            return "unauthorized";
        case GoodbyeReason::PAIRING_REQUIRED:
            return "pairing_required";
        case GoodbyeReason::CONCURRENT_ATTEMPT:
            return "concurrent_attempt";
        case GoodbyeReason::UNPAIRED:
            return "unpaired";
    }
    return "unknown";
}

/// Holds the server key and client records for this process only: every case starts with a
/// server that knows no client.
class MemoryPersistence : public PersistenceProvider {
public:
    std::optional<std::vector<uint8_t>> load_blob(const std::string& key) override {
        std::lock_guard<std::mutex> lock(this->mutex_);
        const auto found = this->blobs_.find(key);
        if (found == this->blobs_.end()) {
            return std::nullopt;
        }
        return found->second;
    }

    bool save_blob(const std::string& key, const uint8_t* data, size_t len) override {
        std::lock_guard<std::mutex> lock(this->mutex_);
        this->blobs_[key].assign(data, data + len);
        return true;
    }

private:
    std::mutex mutex_;
    std::map<std::string, std::vector<uint8_t>> blobs_;
};

std::string goodbye_suffix(const std::optional<GoodbyeReason>& reason) {
    return reason.has_value() ? std::string(" (reason ") + goodbye_reason_name(*reason) + ")" : "";
}

void write_ready(const Args& args, const std::string& server_id, const std::string& url) {
    JsonDocument ready;
    ready["status"] = "ready";
    ready["server_id"] = server_id;
    ready["server_name"] = args.server_name;
    ready["scenario_id"] = args.scenario_id;
    ready["initiator_role"] = args.initiator_role;
    ready["url"] = url;
    write_json_file(args.ready, ready);
}

}  // namespace

class ServerSession::Listener : public ServerListener {
public:
    void on_client_connected(const ClientInfo& client) override {
        std::lock_guard<std::mutex> lock(this->mutex_);
        if (!this->client_.has_value()) {
            this->client_ = client;
        }
        this->changed_.notify_all();
    }

    void on_client_disconnected(const ClientInfo& client,
                                const ClientDisconnect& disconnect) override {
        this->report("client " + client.name + " disconnected: " +
                     disconnect_reason_name(disconnect.reason) +
                     goodbye_suffix(disconnect.goodbye_reason));
    }

    void on_approval_needed(const ClientInfo& client) override {
        this->report("client " + client.name + " is held unapproved for playback");
    }

    void on_pairing_required(const ClientInfo& client) override {
        this->report("client " + client.name +
                     " does not enable unpaired access, so it is held until paired");
    }

    void on_credential_mismatch(const ClientInfo& client) override {
        this->report("client " + client.name +
                     " could not use the PSK the server holds and fell back to the Sentinel");
    }

    void on_error(const ServerError& error) override {
        std::string line = error_kind_name(error.kind);
        if (!error.url.empty()) {
            line += " for " + error.url;
        }
        this->report(line + goodbye_suffix(error.goodbye_reason));
    }

    void on_drained(GroupId /*group*/, InputId /*input*/, DrainId drain,
                    DrainOutcome outcome) override {
        std::lock_guard<std::mutex> lock(this->mutex_);
        this->drains_[drain.value] = outcome;
        this->changed_.notify_all();
    }

    std::optional<ClientInfo> wait_for_client(Deadline deadline) {
        std::unique_lock<std::mutex> lock(this->mutex_);
        this->changed_.wait_until(lock, deadline, [this] { return this->client_.has_value(); });
        return this->client_;
    }

    std::optional<DrainOutcome> wait_for_drain(DrainId drain, Deadline deadline) {
        std::unique_lock<std::mutex> lock(this->mutex_);
        this->changed_.wait_until(lock, deadline,
                                  [&] { return this->drains_.count(drain.value) != 0; });
        const auto found = this->drains_.find(drain.value);
        return found == this->drains_.end() ? std::nullopt
                                            : std::optional<DrainOutcome>(found->second);
    }

    std::vector<std::string> reports() const {
        std::lock_guard<std::mutex> lock(this->mutex_);
        return this->reports_;
    }

private:
    void report(const std::string& line) {
        std::cerr << "listener: " << line << '\n';
        std::lock_guard<std::mutex> lock(this->mutex_);
        this->reports_.push_back(line);
    }

    mutable std::mutex mutex_;
    std::condition_variable changed_;
    std::optional<ClientInfo> client_;
    std::map<uint64_t, DrainOutcome> drains_;
    std::vector<std::string> reports_;
};

ServerSession::ServerSession(const Args& args, TraceRecorder& recorder)
    : args_(args),
      recorder_(recorder),
      listener_(std::make_unique<Listener>()),
      provider_(std::make_unique<MemoryPersistence>()),
      server_(std::make_unique<SendspinServer>()) {}

ServerSession::~ServerSession() {
    this->stop();
}

void ServerSession::start() {
    ServerConfig config;
    config.name = this->args_.server_name;
    config.advertise = false;
    config.browse = false;
    config.listen_port = static_cast<uint16_t>(this->args_.port);
    config.path = "/sendspin";
    config.approve_all_clients = true;
    // The library encodes Opus only when the host opts in; the adapter offers every codec it can
    // produce, so a player that lists Opus first is streamed it (server-initiated-opus)
    config.enable_opus = true;
    config.trace = [this](const TracedMessage& message) { this->recorder_.on_message(message); };
    config.trace_writes = true;
    config.log_level = LogLevel::INFO;
    config.log = [](LogLevel /*level*/, std::string_view line) {
        std::cerr << "sendspin-server-cpp: " << line << '\n';
    };
    const StartResult result = this->server_->start(config, this->provider_.get(),
                                                    this->listener_.get());
    if (result != StartResult::OK) {
        throw std::runtime_error("sendspin-server-cpp did not start: StartResult " +
                                 std::to_string(static_cast<int>(result)));
    }
}

void ServerSession::stop() {
    if (this->server_ == nullptr) {
        return;
    }
    this->server_->stop();
    // The destructor joins the server's threads, the dispatch thread included, so every traced
    // message has reached the recorder once it returns.
    this->server_.reset();
}

std::string ServerSession::url() const {
    return "ws://127.0.0.1:" + std::to_string(this->args_.port) + "/sendspin";
}

std::string ServerSession::server_id() const {
    return this->server_ == nullptr ? std::string() : this->server_->server_id();
}

SendspinServer& ServerSession::server() {
    return *this->server_;
}

void ServerSession::open_connection(Deadline deadline) {
    if (this->args_.initiator_role == "client") {
        JsonDocument registry = read_json_file(this->args_.registry);
        registry[this->args_.server_name]["url"] = this->url();
        write_json_file(this->args_.registry, registry);
        write_ready(this->args_, this->server_id(), this->url());
        return;
    }
    write_ready(this->args_, this->server_id(), this->url());
    for (;;) {
        JsonDocument registry = read_json_file(this->args_.registry);
        JsonVariantConst client_url = registry[this->args_.client_name]["url"];
        if (client_url.is<const char*>()) {
            ConnectOptions options;
            options.retry_until_connected = true;
            if (!this->server_->connect_url(client_url.as<const char*>(), options)) {
                throw std::runtime_error(std::string("connect_url() refused ") +
                                         client_url.as<const char*>());
            }
            return;
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            throw std::runtime_error("client " + this->args_.client_name +
                                     " never registered a URL to dial in " + this->args_.registry);
        }
        std::this_thread::sleep_for(REGISTRY_POLL_INTERVAL);
    }
}

ClientInfo ServerSession::wait_for_client(Deadline deadline) {
    const std::optional<ClientInfo> client = this->listener_->wait_for_client(deadline);
    if (client.has_value()) {
        return *client;
    }
    const std::optional<ConnectionRecord> record = this->recorder_.snapshot();
    std::string reason = !record.has_value()
                             ? "no client completed the handshake with a client/hello"
                             : "the client sent client/hello but never took the connection with "
                               "a client/state after server/activate";
    for (const std::string& line : this->listener_->reports()) {
        reason += "; " + line;
    }
    throw std::runtime_error(reason);
}

std::optional<DrainOutcome> ServerSession::wait_for_drain(DrainId drain, Deadline deadline) {
    return this->listener_->wait_for_drain(drain, deadline);
}

std::vector<std::string> ServerSession::listener_reports() const {
    return this->listener_->reports();
}

}  // namespace conformance
