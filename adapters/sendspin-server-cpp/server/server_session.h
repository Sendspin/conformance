#pragma once

#include "args.h"
#include "trace_recorder.h"

#include <sendspin/server/server.h>

#include <condition_variable>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace conformance {

/// The sendspin-server-cpp server for one case: started without mDNS on the harness's port,
/// approving every client for playback, with the trace feeding a TraceRecorder.
class ServerSession {
public:
    ServerSession(const Args& args, TraceRecorder& recorder);
    ~ServerSession();
    ServerSession(const ServerSession&) = delete;
    ServerSession& operator=(const ServerSession&) = delete;

    /// Starts the server. Throws std::runtime_error when it does not start.
    void start();

    /// Stops the server and waits for its threads, so the trace has delivered every message.
    void stop();

    /// The URL clients connect to
    std::string url() const;

    /// The server_id clients see; empty once stopped
    std::string server_id() const;

    sendspin::server::SendspinServer& server();

    /// Opens the case connection in the scenario's direction: registers url() under the server
    /// name and writes the ready file for a client-initiated scenario, or writes the ready file
    /// and dials the URL the client registers under its name for a server-initiated one.
    /// Throws std::runtime_error at the deadline.
    void open_connection(Deadline deadline);

    /// Waits for a client to take the connection (ServerListener::on_client_connected()).
    /// Throws std::runtime_error at the deadline, naming what the server reported instead.
    sendspin::server::ClientInfo wait_for_client(Deadline deadline);

    /// @return the drain's outcome, or nullopt at the deadline
    std::optional<sendspin::server::DrainOutcome> wait_for_drain(sendspin::server::DrainId drain,
                                                                 Deadline deadline);

    /// What the listener reported that explains a client not playing: errors, a client held
    /// for pairing or approval, a disconnect
    std::vector<std::string> listener_reports() const;

private:
    class Listener;

    const Args& args_;
    TraceRecorder& recorder_;
    std::unique_ptr<Listener> listener_;
    std::unique_ptr<sendspin::server::PersistenceProvider> provider_;
    std::unique_ptr<sendspin::server::SendspinServer> server_;
};

}  // namespace conformance
