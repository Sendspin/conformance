#include "fixture.h"

#include <micro_flac/flac_decoder.h>
#include <openssl/evp.h>

#include <algorithm>
#include <fstream>
#include <iterator>
#include <stdexcept>

namespace conformance {

namespace {

constexpr float S16_FULL_SCALE = 32768.0F;

std::vector<uint8_t> read_file(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        throw std::runtime_error("cannot open fixture " + path);
    }
    return {std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
}

std::string canonical_pcm_sha256(const std::vector<int16_t>& samples) {
    std::vector<float> floats(samples.size());
    for (size_t i = 0; i < samples.size(); ++i) {
        floats[i] = static_cast<float>(samples[i]) / S16_FULL_SCALE;
    }
    // The canonical hash is over little-endian float32, which every supported host stores.
    return sha256_hex(reinterpret_cast<const uint8_t*>(floats.data()),
                      floats.size() * sizeof(float));
}

}  // namespace

std::string sha256_hex(const uint8_t* data, size_t size) {
    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int digest_size = 0;
    if (EVP_Digest(data, size, digest, &digest_size, EVP_sha256(), nullptr) != 1) {
        throw std::runtime_error("SHA-256 failed");
    }
    static const char HEX[] = "0123456789abcdef";
    std::string out;
    out.reserve(size_t{digest_size} * 2);
    for (unsigned int i = 0; i < digest_size; ++i) {
        out.push_back(HEX[digest[i] >> 4]);
        out.push_back(HEX[digest[i] & 0x0F]);
    }
    return out;
}

Fixture decode_fixture(const std::string& path, double max_seconds) {
    const std::vector<uint8_t> file = read_file(path);
    Fixture fixture;
    fixture.path = path;
    fixture.source_flac_sha256 = sha256_hex(file.data(), file.size());

    micro_flac::FLACDecoder decoder;
    std::vector<int32_t> block;
    uint64_t max_frames = 0;
    size_t offset = 0;
    for (;;) {
        size_t consumed = 0;
        size_t decoded = 0;
        const micro_flac::FLACDecoderResult result =
            decoder.decode(file.data() + offset, file.size() - offset, block.data(), block.size(),
                           consumed, decoded);
        offset += consumed;
        if (result == micro_flac::FLAC_DECODER_HEADER_READY) {
            const micro_flac::FLACStreamInfo& info = decoder.get_stream_info();
            fixture.sample_rate = info.sample_rate();
            fixture.channels = static_cast<uint8_t>(info.num_channels());
            block.resize(size_t{info.max_block_size()} * info.num_channels());
            max_frames = static_cast<uint64_t>(fixture.sample_rate * max_seconds);
            continue;
        }
        if (result == micro_flac::FLAC_DECODER_SUCCESS) {
            // The int32_t output is left-justified, so the top 16 bits are the s16 sample.
            for (size_t i = 0; i < decoded; ++i) {
                fixture.samples.push_back(static_cast<int16_t>(block[i] >> 16));
            }
            if (fixture.samples.size() / fixture.channels >= max_frames) {
                break;
            }
            continue;
        }
        if (result == micro_flac::FLAC_DECODER_END_OF_STREAM ||
            (result == micro_flac::FLAC_DECODER_NEED_MORE_DATA && offset >= file.size())) {
            break;
        }
        if (result != micro_flac::FLAC_DECODER_NEED_MORE_DATA) {
            throw std::runtime_error("cannot decode fixture " + path + ": micro-flac error " +
                                     std::to_string(static_cast<int>(result)));
        }
    }
    if (fixture.channels == 0 || fixture.samples.empty()) {
        throw std::runtime_error("fixture " + path + " decoded to no audio");
    }
    fixture.samples.resize(std::min<size_t>(fixture.samples.size(),
                                            static_cast<size_t>(max_frames) * fixture.channels));
    fixture.frame_count = fixture.samples.size() / fixture.channels;
    fixture.source_pcm_sha256 = canonical_pcm_sha256(fixture.samples);
    return fixture;
}

}  // namespace conformance
