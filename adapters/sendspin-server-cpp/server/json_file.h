#pragma once

#include <ArduinoJson.h>

#include <optional>
#include <string>

namespace conformance {

/// Writes doc as indented JSON, replacing the file whole so a reader never sees part of it.
/// Throws std::runtime_error when it cannot be written.
void write_json_file(const std::string& path, const JsonDocument& doc);

/// @return the JSON in the file, or an empty document when it is absent or does not parse
JsonDocument read_json_file(const std::string& path);

/// @return the parsed text, or an empty document, which reads as null, for none or text that
/// does not parse
JsonDocument parse_json(const std::optional<std::string>& text);

}  // namespace conformance
