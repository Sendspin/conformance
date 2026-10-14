#pragma once

#include <string>

namespace conformance {

/// The arguments the harness passes to a server adapter (adapters/README.md, runner.py).
struct Args {
    std::string summary;
    std::string ready;
    std::string registry;
    std::string fixture;
    std::string scenario_id;
    std::string initiator_role = "server";
    std::string preferred_codec = "pcm";
    std::string client_name;
    std::string client_id;
    std::string server_id = "conformance-server";
    std::string server_name = "Sendspin Conformance Server";
    double timeout_seconds = 30.0;
    double clip_seconds = 5.0;
    int port = 8927;
};

}  // namespace conformance
