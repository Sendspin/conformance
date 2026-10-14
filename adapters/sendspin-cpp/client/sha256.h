#pragma once

#include <openssl/evp.h>

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>

inline std::string hex_lower(const unsigned char* data, size_t len) {
    static const char hex[] = "0123456789abcdef";
    std::string out;
    out.reserve(len * 2);
    for (size_t i = 0; i < len; i++) {
        out.push_back(hex[data[i] >> 4]);
        out.push_back(hex[data[i] & 0x0F]);
    }
    return out;
}

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
    EVP_MD_CTX* ctx_;
};
