// Reads the recorder's files ("tapes"), whose format is described in recorder/tape.py.
#pragma once

#include <simdjson.h>
#include <zlib.h>

#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace engine {

// One line of a tape: a message exactly as an exchange sent it, or an event the recorder wrote
// about itself (connected, sent, rest_snapshot, planned_reconnect, ...).
struct Record {
  enum class Kind : std::uint8_t { message, event };
  Kind kind = Kind::message;
  std::int64_t wall_ns = 0;  // calendar time when received, ns since 1970 UTC; can jump
  std::int64_t mono_ns = 0;  // a clock that only moves forward
  std::string_view event;    // the event's name (events only)
  // The message text, or an event's "raw" field (a downloaded book, a command that was sent);
  // empty if the event has none. It is followed by enough spare bytes for simdjson to parse it
  // in place. Valid until the next call to next().
  simdjson::padded_string_view body;
  bool binary = false;  // the message was not valid text, so the recorder stored it as base64
};

class TapeReader {
 public:
  // Opens a tape. Throws std::runtime_error if the file cannot be opened.
  explicit TapeReader(const std::filesystem::path& path);
  ~TapeReader();
  TapeReader(const TapeReader&) = delete;
  TapeReader& operator=(const TapeReader&) = delete;
  TapeReader(TapeReader&&) = delete;
  TapeReader& operator=(TapeReader&&) = delete;

  // Reads the next complete record into `record`. Returns false at the end of the readable
  // data. A file cut off by a crash is read up to its last complete line; damaged bytes at
  // the end (which a power cut can leave) end the reading without an error.
  bool next(Record& record);

  // Once next() has returned false: true if the file was finished properly (the gzip end was
  // found and its checksum matched).
  bool complete() const { return complete_; }
  // Once next() has returned false: true if bytes that could not be read were found, as
  // opposed to a file that simply stops.
  bool damaged() const { return damaged_; }

 private:
  bool fill();  // decompresses more text; false if none
  bool parse_line(std::size_t begin, std::size_t end, Record& record);

  std::unique_ptr<std::FILE, int (*)(std::FILE*)> file_;
  z_stream zlib_{};
  bool zlib_ready_ = false;
  std::vector<unsigned char> input_;  // compressed bytes read from the file
  // Decompressed text. [begin_, end_) is not yet returned; the buffer always extends at least
  // SIMDJSON_PADDING bytes past end_, so a line can be parsed where it lies.
  std::vector<char> text_;
  std::size_t begin_ = 0;
  std::size_t end_ = 0;
  std::size_t searched_ = 0;  // [begin_, searched_) is known to contain no line break
  bool finished_ = false;     // nothing more will be decompressed
  bool complete_ = false;
  bool damaged_ = false;
  simdjson::ondemand::parser parser_;
  std::string event_;  // the current record's event name
  std::string body_;   // the current record's message or "raw" text, unescaped, with padding
};

}  // namespace engine
