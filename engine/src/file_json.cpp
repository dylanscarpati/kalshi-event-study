#include "file_json.hpp"

#include <array>
#include <cstdio>

namespace engine {
namespace {

std::string json_string(std::string_view text) {
  std::string out = "\"";
  for (const char c : text) {
    if (c == '"' || c == '\\') {
      out += '\\';
      out += c;
    } else if (static_cast<unsigned char>(c) < 0x20) {
      std::array<char, 8> code{};
      std::snprintf(code.data(), code.size(), "\\u%04x", static_cast<unsigned>(c));
      out += code.data();
    } else {
      out += c;
    }
  }
  return out + '"';
}

std::string json_number(const std::optional<std::int64_t>& value) {
  return value ? std::to_string(*value) : "null";
}

}  // namespace

std::string file_json(const FileSummary& file) {
  const Integrity& in = file.integrity;
  std::string out = R"({"feed":)" + json_string(file.feed) + R"(,"file":)" + json_string(file.file);
  out += R"(,"start_ns":)" + json_number(in.start_ns) + R"(,"end_ns":)" + json_number(in.end_ns);
  out += R"(,"ready_ns":)" + json_number(in.ready_ns);
  out += R"(,"invalid_from_ns":)" + json_number(in.invalid_from_ns) + R"(,"break_ns":[)";
  for (std::size_t i = 0; i < in.break_ns.size(); ++i) {
    out += (i > 0 ? "," : "") + std::to_string(in.break_ns[i]);
  }
  out += R"(],"closed_ns":)" + json_number(in.closed_ns) + R"(,"ended":)" + json_string(in.ended);
  out += R"(,"unplanned":)" + std::string(in.unplanned() ? "true" : "false");
  out += R"(,"complete":)" + std::string(file.complete ? "true" : "false");
  out += R"(,"damaged":)" + std::string(file.damaged ? "true" : "false") + R"(,"counts":{)";
  bool first = true;
  for (const auto& [name, count] : in.counts) {
    out += (first ? "" : ",") + json_string(name) + ":" + std::to_string(count);
    first = false;
  }
  out += R"(},"problems":[)";
  for (std::size_t i = 0; i < in.problems.size(); ++i) {
    out += (i > 0 ? "," : "") + std::string("[") + std::to_string(in.problems[i].first) + "," +
           json_string(in.problems[i].second) + "]";
  }
  return out + "]}";
}

}  // namespace engine
