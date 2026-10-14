#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace conformance {

/// The shared source clip, decoded to the canonical signed 16-bit PCM that
/// conformance.flac.decode_fixture() produces, so both hash the same audio.
struct Fixture {
    std::string path;
    /// Interleaved little-endian s16
    std::vector<int16_t> samples;
    uint32_t sample_rate{0};
    uint8_t channels{0};
    uint64_t frame_count{0};
    std::string source_flac_sha256;
    /// SHA-256 of the samples as little-endian float32, each divided by 32768
    std::string source_pcm_sha256;
};

/// Decodes at most max_seconds of the FLAC file at path. A sample deeper than
/// 16 bits keeps its top 16 bits, as libswresample's conversion does.
/// Throws std::runtime_error when the file cannot be read or decoded.
Fixture decode_fixture(const std::string& path, double max_seconds);

/// Returns the lowercase hex SHA-256 of data.
std::string sha256_hex(const uint8_t* data, size_t size);

}  // namespace conformance
