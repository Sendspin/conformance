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

    std::string metadata_title = "Almost Silent";
    std::string metadata_artist = "Sendspin Conformance";
    std::string metadata_album_artist = "Sendspin";
    std::string metadata_album = "Protocol Fixtures";
    std::string metadata_artwork_url = "https://example.invalid/almost-silent.jpg";
    int metadata_year = 2026;
    int metadata_track = 1;
    int metadata_track_progress = 12000;
    int metadata_track_duration = 180000;
    int metadata_playback_speed = 1000;

    std::string controller_command = "next";
    std::string controller_repeat = "all";
    std::string controller_shuffle = "false";

    std::string artwork_format = "jpeg";
    int artwork_width = 256;
    int artwork_height = 256;
};

}  // namespace conformance
