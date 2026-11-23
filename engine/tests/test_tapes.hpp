// Test helpers: write small tapes in the recorder's format (see recorder/tape.py).
#pragma once

#include <gtest/gtest.h>
#include <zlib.h>

#include <array>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <string_view>

namespace engine::testing {

// Text as a JSON string literal, quotes included.
inline std::string json_string(std::string_view text) {
  std::string out = "\"";
  for (const char c : text) {
    switch (c) {
      case '"':
        out += "\\\"";
        break;
      case '\\':
        out += "\\\\";
        break;
      case '\n':
        out += "\\n";
        break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          std::array<char, 8> code{};
          std::snprintf(code.data(), code.size(), "\\u%04x", static_cast<unsigned>(c));
          out += code.data();
        } else {
          out += c;
        }
    }
  }
  return out + '"';
}

// One tape line holding a message, as the recorder writes it.
inline std::string message_line(std::int64_t wall_ns, std::string_view raw) {
  return R"({"wall_ns":)" + std::to_string(wall_ns) + R"(,"mono_ns":)" +
         std::to_string(wall_ns - 1'000'000'000) + R"(,"raw":)" + json_string(raw) + "}\n";
}

// One tape line holding a recorder event; `raw` is included only if not empty.
inline std::string event_line(std::int64_t wall_ns, std::string_view name,
                              std::string_view raw = {}) {
  std::string line = R"({"wall_ns":)" + std::to_string(wall_ns) + R"(,"mono_ns":)" +
                     std::to_string(wall_ns - 1'000'000'000) + R"(,"event":)" + json_string(name);
  if (!raw.empty()) {
    line += R"(,"raw":)" + json_string(raw);
  }
  return line + "}\n";
}

// Compresses text into gzip bytes, with the same sync flush the recorder uses.
class GzipWriter {
 public:
  GzipWriter() {
    if (deflateInit2(&zlib_, 6, Z_DEFLATED, 31, 8, Z_DEFAULT_STRATEGY) != Z_OK) {
      throw std::runtime_error("deflateInit2 failed");
    }
  }
  ~GzipWriter() { deflateEnd(&zlib_); }
  GzipWriter(const GzipWriter&) = delete;
  GzipWriter& operator=(const GzipWriter&) = delete;

  void write(std::string_view text) { run(text, Z_NO_FLUSH); }
  // Makes everything written so far decodable; returns the number of bytes produced.
  std::size_t sync_flush() {
    run({}, Z_SYNC_FLUSH);
    return bytes_.size();
  }
  // Writes the gzip end (checksum and length) and returns all the bytes.
  const std::string& finish() {
    run({}, Z_FINISH);
    return bytes_;
  }
  const std::string& bytes() const { return bytes_; }

 private:
  void run(std::string_view text, int flush) {
    // zlib's interface takes a non-const pointer but does not write through next_in.
    zlib_.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(text.data()));
    zlib_.avail_in = static_cast<uInt>(text.size());
    std::array<unsigned char, 1 << 16> chunk{};
    do {
      zlib_.next_out = chunk.data();
      zlib_.avail_out = static_cast<uInt>(chunk.size());
      deflate(&zlib_, flush);
      bytes_.append(reinterpret_cast<const char*>(chunk.data()), chunk.size() - zlib_.avail_out);
    } while (zlib_.avail_out == 0);
  }

  z_stream zlib_{};
  std::string bytes_;
};

// A file path unique to the running test.
inline std::filesystem::path test_path(std::string_view name) {
  const auto* test = ::testing::UnitTest::GetInstance()->current_test_info();
  std::filesystem::path dir = std::filesystem::path(::testing::TempDir()) / "engine_tests" /
                              test->test_suite_name() / test->name();
  std::filesystem::create_directories(dir);
  return dir / name;
}

inline void write_file(const std::filesystem::path& path, std::string_view bytes) {
  std::ofstream out(path, std::ios::binary | std::ios::trunc);
  out.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
}

}  // namespace engine::testing
