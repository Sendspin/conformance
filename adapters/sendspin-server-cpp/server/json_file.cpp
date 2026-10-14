#include "json_file.h"

#include <cstdio>
#include <fstream>
#include <iterator>
#include <stdexcept>

namespace conformance {

void write_json_file(const std::string& path, const JsonDocument& doc) {
    std::string text;
    serializeJsonPretty(doc, text);
    text.push_back('\n');
    const std::string staging = path + ".tmp";
    {
        std::ofstream out(staging, std::ios::binary | std::ios::trunc);
        out << text;
        if (!out) {
            throw std::runtime_error("cannot write " + staging);
        }
    }
    if (std::rename(staging.c_str(), path.c_str()) != 0) {
        throw std::runtime_error("cannot replace " + path);
    }
}

JsonDocument read_json_file(const std::string& path) {
    JsonDocument doc;
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        return doc;
    }
    const std::string text{std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
    if (deserializeJson(doc, text)) {
        doc.clear();
    }
    return doc;
}

JsonDocument parse_json(const std::optional<std::string>& text) {
    JsonDocument doc;
    if (text.has_value() && deserializeJson(doc, *text)) {
        doc.clear();
    }
    return doc;
}

}  // namespace conformance
